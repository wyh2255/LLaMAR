# 导入核心引擎模块（Coordinate、Field、Controller 等核心类）
import core
# 操作系统接口、OpenCV 图像处理、深拷贝工具
import os, cv2, copy
# 从核心引擎导入控制器类
from core import Controller
# 导入渲染函数，用于可视化环境状态
from utils import render
# 导入枚举值读取工具
from misc import read_enum
# 导入深拷贝函数
from copy import deepcopy
# 导入基础环境类，包含动作解析等公共方法
from base_env import SARBaseEnv
# 导入类型标注工具
from typing import List, Tuple, Dict
# 导入路径操作工具
from pathlib import Path
# 导入场景初始化器工厂函数
from Scenes.get_scene_init import get_scene_initializer

# 所有不需要控制器实例的方法都实现在 SARBaseEnv 中
""" Anything method doesn't require controller instance is in SARBaseEnv """

class SAREnv(SARBaseEnv):
    """
    Search and Rescue environment,
    has same interface as AI2Thor env
    thus allowing for exact same use in the baseline files.

    SAR（搜索与救援）环境主类。
    包装 SAR 核心引擎的 Controller，提供与 AI2Thor 环境一致的接口，
    使得 LLaMAR 基线代码可以无缝在两种环境上运行。

    主要职责：
      - 管理智能体的生命周期和状态追踪
      - 生成 LLM 可读的观测文本和状态描述
      - 分发动作执行并收集结果
      - 维护子任务、记忆和历史记录
    """

    """
    Controller docs:

    ------------------------------
    .step(action, action_args)
        Formatting for actions, required action_args:

        'NavigateTo' : to_target_id (location),
        'Move' : direction, # Up, Down, Left, Right
        'Explore',
        'Carry' : from_target_id (person),
        'DropOff' : to_target_id (deposit),
        'StoreSupply' : to_target_id (deposit),
        'UseSupply' : from_target_id (fire), supply_type (on fire)
        'GetSupply' : from_target_id (deposit or reservoir), supply_type (deposit)
        'ClearInventory',
        'NoOp',
    ------------------------------

    ------------------------------
    event={
        'success' : None,
        'global_obs' : None,
        'local_obs' : None,
        'visual_obs' : None, # non-existent for now
        'info' : '',
        }

        global_obs -> unordered list of formatted_objects
        local_obs -> dict. key<->(cardinal direction), value<->(list of formatted_objects @ delta)
            cardinal directions ->(Up,Down),(Left,Right) and all pairs from group 1 and 2
    ------------------------------

    ------------------------------
    Format of formatted_objects
        {
        # FOR ALL
        'position' : x,y position of the object,
        'name' : given name for object,
        'object_id' : id of the object,
        'type' : type of object (class)
        'collidable' : is object collidable,

        # SPECIFIC
        (if flammable) 'intensity' :  'none', 'low', 'medium', 'high',
        (if flammable) 'fire_type' : 'chemical' or 'non-chemical',

        (if fire) 'average_intensity' : 'none', 'low', 'medium', 'high',
        (if fire) 'fire_type' : 'chemical' or 'non-chemical',

        (if person) 'load' : number corresponding to amt of agents needed,
        (if person) 'status' : 'grabbed' or 'grounded',

        (if reservoir) 'resource_type' : 'sand' or 'water',
        (if reservoir) 'inventory' : dict w/ {resource_type : amt} (likely math.inf),

        (if deposit) 'inventory' : dict w/ {resource_type : amt} for all resource types (including person),

        (if absagent) 'inventory' : dict w/ {resource_type : amt} for all resource types (including person),
        }
    ------------------------------
    """
    # --- 类级常量定义 ---
    # 可用智能体名称列表（最多支持 6 个智能体同时运行）
    AGENT_NAMES=['Alice', 'Bob', 'Charlie', 'David', 'Emma', 'Finn']
    # 所有可用动作 = 控制器原生动作 + 自定义探索动作
    AVAILABLE_ACTIONS=Controller.ALL_ACTIONS+['Explore']
    # 可移动的方位方向（上、下、左、右等）
    CARDINAL_DIRECTIONS=Controller.MOVABLE_CARDINAL_DIRECTIONS

    # 环境中所有对象的类型列表（排除 AbsAgent 自身类型），用于构造 LLM 提示
    # To be used in prompt (describing environment), useful variables
    CLASS_TYPES=core.Field.CLASS_TYPES[:-1] # exclude AbsAgent

    # 智能体最大库存容量
    INVENTORY_CAPACITY=core.AbsAgent.INVENTORY_CAPACITY
    # 资源类型列表（首字母大写，如 Sand、Water）
    INVENTORY_TYPES=list([k.capitalize() for k in core.Field.UNREADABLE_TYPE_MAPPER_RESOURCE.keys()])
    # 火灾类型列表（首字母大写，如 Chemical、Non-chemical）
    FIRE_TYPES=list([k.capitalize() for k in core.Field.UNREADABLE_TYPE_MAPPER_FIRE.keys()])
    # 灭火资源类型（默认取前 N 种资源对应 N 种火灾类型）
    EXTINGUISH_TYPES=INVENTORY_TYPES[:len(FIRE_TYPES)] # assumed to be first n -> fire extinguish types
    # 搬运人员所需的最小智能体数量
    MIN_REQUIRED_AGENTS=core.Person.MIN_REQUIRED_AGENTS

    # 火焰从低强度到中强度的步数阈值
    L_TO_M=core.Flammable.L_TO_M
    # 火焰从中强度到高强度的步数阈值
    M_TO_H=core.Flammable.M_TO_H
    # 危急火势强度阈值（达到此值时火焰开始向邻近格子扩散）
    CRITICAL_INTENSITY=read_enum(core.Flammable.CRITICAL_INTENSITY)
    # 强度级别的总数量
    AMT_INTENSITIES=len(list(filter(lambda s : '_' not in s, dir(core.Intensity))))
    # 所有强度级别的可读名称列表
    ALL_INTENSITIES=[read_enum(core.Intensity(i)) for i in range(1, AMT_INTENSITIES+1)]
    # 网格坐标系高度
    GRID_HEIGHT=core.Coordinate.HEIGHT
    # 网格坐标系宽度
    GRID_WIDTH=core.Coordinate.WIDTH


    def __init__(self, num_agents, scene=1, seed=42, save_frames=False):
        """
        初始化 SAR 环境实例。

        设置随机种子、智能体数量、场景编号等基本配置。
        环境的完整初始化（控制器创建、场景加载）在调用 .reset() 时完成。

        参数:
            num_agents (int): 智能体数量，取值范围 1-6
            scene (int): 场景编号，默认为 1
            seed (int): 随机种子，确保实验可重复
            save_frames (bool): 是否在每一步保存渲染帧图像
        """
        # NOTE: AutoConfig NOT needed
        self.seed=seed
        SARBaseEnv.seed=self.seed
        self.num_agents=num_agents
        self.scene=scene
        self.save_frames=save_frames

        assert 1<=num_agents<=len(SAREnv.AGENT_NAMES), f"Amount of agents <1 or >maximum."
        self.agent_names=SAREnv.AGENT_NAMES[:num_agents]
        # 初始化为 False，在 .reset() 后变为 True，标记环境可用
        # only true when .reset()
        self.initialized=False


    def reset(self):
        """
        Resets and initializes the environment (controller)

        重置并初始化环境。清理所有状态，重新加载场景，
        创建控制器、检查器和所有智能体状态追踪变量。
        返回格式化后的初始输入字典（包含任务描述）。

        返回:
            str: 格式化后的初始输入字典字符串
        """

        # --- 标记环境已初始化 ---
        # 标记环境已初始化，允许后续执行 .step() 操作
        self.initialized=True

        # --- 获取场景初始化器和检查器 ---
        # 根据场景编号获取场景初始化器和任务完成度检查器
        scene_initializer,checker=get_scene_initializer(self.scene)

        # 创建场景初始化器实例，获取任务超时时间和任务描述
        scene_initializer_instance=scene_initializer.SceneInitializer()
        self.task_timeout=scene_initializer_instance.task_timeout
        self.task=scene_initializer_instance.get_task()

        # --- 初始化控制器和环境 ---
        # 初始化控制器和场景参数（包含所有对象的位置和属性）
        self.controller, pg_params=scene_initializer_instance.preinit(
            self.num_agents, self.agent_names, self.seed
        )
        # 保存场景参数，用于对象动作处理和检查器初始化
        self.object_dict = pg_params # used in obj actions & init-ing checker
        # 创建任务完成度检查器
        self.checker = checker.Checker(copy.deepcopy(self.object_dict))

        # --- 初始化状态追踪变量 ---
        # 初始化输入字典，填充任务描述
        self.input_dict = {}
        self.input_dict["Task"] = self.task

        # 初始化子任务和记忆追踪变量
        self.open_subtasks=[]             # 待完成的开放子任务列表
        self.closed_subtasks=[]           # 已完成的子任务列表
        self.subtasks=[[] for _ in range(self.num_agents)]   # 每个智能体的子任务
        self.memory=[[] for _ in range(self.num_agents)]     # 每个智能体的记忆

        # 每个智能体的步数计数器
        self.step_num = [0] * self.num_agents

        # 初始化各智能体的历史记录
        self.all_obs_dict = { self.agent_names[i]: [] for i in range(self.num_agents) }            # 观察历史
        self.action_history = { self.agent_names[i]: [] for i in range(self.num_agents) }           # 动作历史
        self.action_success_history = { self.agent_names[i]: [] for i in range(self.num_agents) }   # 动作成功/失败历史
        self.agent_failure_acts = { self.agent_names[i]: set([]) for i in range(self.num_agents) }  # 失败动作集合（用 set 避免重复记录）
        self.step_nums_history = { self.agent_names[i]: [] for i in range(self.num_agents) }        # 步数历史
        self.previous_success = { self.agent_names[i]: [] for i in range(self.num_agents) }         # 上一步成功状态

        # --- 设置初始状态 ---
        # 更新环境状态，初始时所有智能体均标记为"尚未执行任何动作"
        self.update_current_state(["I have not taken any actions yet"]*self.num_agents)

        # 设置渲染图像和动作记录的保存路径
        base_path=f"scene_{self.scene}/"
        self.render_image_path=Path(f"render/{self.num_agents}_agents/seed_{self.seed}/{base_path}")
        self.action_dir_path=Path(f"actions/{self.num_agents}_agents/seed_{self.seed}/{base_path}")

        return SARBaseEnv.convert_dict_to_string(self.input_dict)

    def update_current_state(self, act_texts: List[str]):
        """
        Update the input dictionary with the current state of the environment

        更新输入字典，为每个智能体设置最新观测、状态、前一步动作等信息。
        这些信息将用于构造 LLM 提示。

        参数:
            act_texts (List[str]): 每个智能体上一步执行动作的文本描述列表
        """
        # NOTE: Since we get observation after we do all the steps, we'll have non-stale observations

        # --- 逐个智能体更新状态 ---
        for agent_idx in range(self.num_agents):
            agent_name = self.agent_names[agent_idx]

            # 生成该智能体的观测文本和全局观测列表
            obs_text,global_obs_list = self.generate_obs_text(agent_idx)
            # 获取智能体状态描述（位置、朝向、库存等）
            state=self.get_agent_state(agent_idx)  # textual description of the agent's state (heading, position, inventory, etc)
            # 获取该智能体之前失败动作的文本描述
            act_failure_text=self.get_act_failure_text(self.agent_failure_acts[agent_name], agent_idx)

            # --- 填充输入字典 ---

            self.input_dict[agent_name + "'s observation"] = obs_text               # 当前观测
            self.input_dict[agent_name + "'s state"] = state                        # 当前状态
            # MISSING: if use_act_summariser, make [agent_name + "'s feasible actions"] <- get_feasible_actions(global_obs_list, agent_idx)
            self.input_dict[agent_name + "'s previous observation"] = self.all_obs_dict[agent_name]  # 上一步的观测
            self.input_dict[agent_name + "'s previous action"] = act_texts[agent_idx]   # 上一步的动作
            self.input_dict[agent_name + "'s previous failures"]=act_failure_text       # 之前的失败信息

            # 保存当前全局观测列表供下一步使用
            self.all_obs_dict[self.agent_names[agent_idx]]=global_obs_list # add previous history

        # MISSING: separate subtasks
        # MISSING: separate memories
        # 目前子任务和记忆是所有智能体共享的（后续可扩展为独立）
        # default to shared for both of these
        self.input_dict["Robots' subtasks"]=self.subtasks[0]          # 子任务
        self.input_dict["Robots' combined memory"]=self.memory[0]     # 组合记忆
        self.input_dict["Robots' open subtasks"]=self.open_subtasks   # 待完成子任务
        self.input_dict["Robots' completed subtasks"]=self.closed_subtasks  # 已完成子任务

    def generate_obs_text(self, agent_idx):
        """
        生成智能体的观测文本，供 LLM 提示使用。

        观测分为两个层次:
          1) 局部观测 (local_obs) — 智能体周围各方向的可观察物体
          2) 全局观测 (global_obs) — 智能体视野内所有已知物体的可读描述

        参数:
            agent_idx (int): 智能体索引
        返回:
            obs_text (str): 格式化的完整观测文本
            global_obs_list (str): 仅含名称的全局观测列表（用于历史记录）
        """
        dct=self.controller.get_observation(agent_idx)
        lobs=dct['local_obs']   # 局部观测：方向 -> 物体列表
        gobs=dct['global_obs']  # 全局观测：所有可观测物体的详细列表

        # ====== 局部观测预处理 ======
        # lobs 是"方向 -> 物体字典列表"的映射
        # 转换为"方向 -> 可读字符串列表"（可燃物显示强度，否则显示 'Obstacle'）
        # --- preprocessing ---
        # lobs is delta -> obj dict list
        # change to delta -> str of string_description (if flammable & not blocked) else 'obstacle' list
        for direction,ldcts in lobs.items():
            def is_flammable(d):
                if d['type']=='Flammable':
                    return f"Flammable of fire (intensity {d['intensity']}): {d['parent_fire']}"
                return 'Obstacle'
            ldcts=list(map(is_flammable, ldcts))

            v=ldcts # value to replace lobs[direction] dict with
            # 假设 Fire 等抽象对象不在局部观测中（因此不需要过滤）
            # we're assuming abstract objects such as fire are not part of lobs (thus no need for filter)

            # 如果有障碍物则整体标记为 Obstacle，空方向标记为 Empty
            if ('Obstacle' in ldcts): v=['Obstacle']
            elif len(ldcts)==0: v=['Empty']

            lobs[direction]=str(v)

        # 如果某个方向不在局部观测中，说明是墙壁（障碍物）
        # if a direction isn't in lobs, it's a wall (obstacle)
        for direction in Controller.MOVABLE_CARDINAL_DIRECTIONS:
            if direction not in lobs.keys():
                lobs[direction]=['Obstacle']

        # 确保字典的顺序固定（按照 MOVABLE_CARDINAL_DIRECTIONS 的顺序）
        lobs={direction: lobs[direction] for direction in Controller.MOVABLE_CARDINAL_DIRECTIONS}
        # -------------------

        # ====== 全局观测预处理 ======
        # gobs 是物体字典列表，转换为可读字符串和名称列表
        readable=lambda d : d['string_description']
        get_names=lambda d: d['name']

        # --- preprocessing ---
        # 分别提取全局观测的描述文本和名称列表
        gobs_names=list(map(get_names, gobs))
        gobs=list(map(readable, gobs))

        # @here
        # 过滤掉非区域名（_Region）的火对象，避免智能体看到重复/冗余的位置信息
        # NOTE: Filter out fire from the observation here - to avoid having the agent
        #       have center (could have double fire) / non-center (redundant) positions to the fire (since we already have regions)
        gobs_names=list(filter(lambda s : ('Fire' not in s) or ('_Region' in s), gobs_names))

        # ====== 构造文本 ======
        # 添加局部观测
        str_lobs=SARBaseEnv.convert_dict_to_string(lobs, pre='\t')
        obs_text=f"\n\tDirectly around me, I can see:\n\t{str_lobs}," # {'Left' : ['Obstacle'], 'Right' : ['Empty'], 'UpRight' : ['Flammable fire: GreatFire'], ...}
        # 添加全局观测描述
        obs_text+=f"\n\tGlobally, I can see: {str(gobs)}," # ['Flammable object named GreatFire_Region_1 with an intensity of Low', ...]
        # 添加全局观测名称列表
        obs_text+=f"\n\tNames: {str(gobs_names)}" # ['GreatFire', 'LostTimmy']

        # 保存名称列表作为历史记录（用于下一步的"上一步观测"）
        global_obs_list=str(gobs_names)

        return obs_text,global_obs_list

    def get_agent_state(self, agent_idx: int):
        """
        Return a string describing the agent's state

        返回描述智能体当前状态的文本，包括位置坐标和库存信息。
        该文本将填入 LLM 提示的 "{agent_name}'s state" 字段。

        参数:
            agent_idx (int): 智能体索引
        返回:
            str: 智能体状态描述，如 "I am at co-ordinates: (5, 3) and I am holding {'Sand': 1, 'Water': 0, 'Person': 0}."
        """
        agent=self.controller.get('agents', agent_idx)
        inventory=self.controller.get_inventory(agent_idx)
        agent_state = f"I am at co-ordinates: {agent.get_position()} and I am holding {inventory}."
        return agent_state

    def update_memory(self, memory : list, agent_idx : str):
        """更新指定智能体的记忆列表。"""
        self.memory[agent_idx]=memory

    def update_subtask(self, subtask : list, agent_idx : str):
        """更新指定智能体的子任务列表。"""
        self.subtasks[agent_idx]=subtask

    def update_plan(self, plan : str):
        """更新全局规划文本。"""
        self.plan=plan

    def get_id(self, name : str):
        """
        根据对象名称获取其唯一 ID。

        参数:
            name (str): 对象的可读名称
        返回:
            str 或 None: 对象的唯一 ID，若名称不存在则返回 None
        """
        # could be None if name doesn't exist
        return self.controller.get_id(name)

    # ------- prompts -------
    def get_planner_llm_input(self):
        """
        Returns the input to the subtask LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[0]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[1]}'s observation: list of objects the {agent_name[1]} is observing,
        Robots' open subtasks: list of subtasks the robots are supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        }}

        返回用于 Planner LLM（子任务规划器）的输入字典。
        包含任务描述、各智能体的观测以及当前子任务进度。
        Planner LLM 根据这些信息将整体任务分解为可执行的子任务列表。

        返回:
            dict: 包含任务和所有智能体观测信息的字典
        """
        # 根据智能体数量提取每个智能体的观测
        # extract the agent_name's observations based on how many agents there are
        planner_llm__input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            planner_llm__input_feats.append(agent_name + "'s observation")
        planner_llm__input_feats.extend(
            ["Robots' open subtasks", "Robots' completed subtasks"]
        )
        return dict((k, self.input_dict[k]) for k in planner_llm__input_feats)

    def get_verifier_llm_input(self):
        """
        Returns the input to the verifier LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[i]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[i]}'s previous action: previous action of the {agent_name[0]},
        Robots' open subtasks: list of subtasks the robots are supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        Robots' combined memory: description of robots' combined memory}}

        返回用于 Verifier LLM（验证器）的输入字典。
        除了任务和观测外，还包含各智能体的状态和上一步动作，
        以及组合记忆信息，供验证器判断子任务完成情况。
        Verifier LLM 根据这些信息确认哪些子任务已经完成。

        返回:
            dict: 包含任务、观测、状态、动作和记忆的字典
        """
        # 根据智能体数量提取每个智能体的观测、状态和上一步动作
        # extract the agent_name's observations based on how many agents there are
        verifier_llm_input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            verifier_llm_input_feats.extend(
                [
                    agent_name + "'s observation",
                    agent_name + "'s state",
                    agent_name + "'s previous action",
                ]
            )
        verifier_llm_input_feats.extend(
            [
                "Robots' open subtasks",
                "Robots' completed subtasks",
                "Robots' combined memory",
            ]
        )
        return dict((k, self.input_dict[k]) for k in verifier_llm_input_feats)

    def get_action_llm_input(self, failure_module=False):
        """
        Returns the input to the subtask LLM
        ### INPUT FORMAT ###
        {{Task: description of the task the robots are supposed to do,
        {agent_name[i]}'s observation: list of objects the {agent_name[0]} is observing,
        {agent_name[i]}'s state: description of {agent_name[0]}'s state,
        {agent_name[i]}'s previous action: description of what {agent_name[0]} did in the previous time step and whether it was successful,
        {agent_name[i]}'s previous failures: if {agent_name[0]}'s few previous actions failed, description of what failed,
        Robots' open subtasks: list of subtasks  supposed to carry out to finish the task. If no plan has been already created, this will be None.
        Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
        Robots' subtask: description of the subtasks the robots were trying to complete in the previous step,
        Robots' combined memory: description of robot's combined memory}}

        返回用于 Actor LLM（动作生成器）的输入字典。
        包含任务、各智能体的观测、状态、上一步动作、失败信息，
        以及子任务和记忆信息。
        Actor LLM 根据这些信息决定每个智能体的下一步动作。

        若 failure_module=True，还会包含失败原因分析字段，
        帮助 LLM 从之前的失败中学习并调整策略。

        参数:
            failure_module (bool): 是否包含失败模块的额外输入（失败原因）
        返回:
            dict: 包含所有动作生成所需信息的字典
        """
        llm_input_feats = ["Task"]
        for i in range(self.num_agents):
            agent_name = self.agent_names[i]
            llm_input_feats.extend(
                [
                    agent_name + "'s observation",
                    agent_name + "'s state",
                    agent_name + "'s previous action",
                    agent_name + "'s previous failures",
                ]
            )
        llm_input_feats.extend(
            [
                "Robots' open subtasks",
                "Robots' completed subtasks",
                "Robots' combined memory",
            ]
        )

        if failure_module:
            # post action / failure module inputs
            # v0 - give failure reason (override environment-given), add logic for next action
            llm_input_feats.extend(
                    [
                        "failure reason",
                        # "logic for next action",
                    ]
            )
        return dict((k, self.input_dict[k]) for k in llm_input_feats)

    def step(self, actions):
        """
        Execute the actions for all the agents in the environment
        Return the observation for all the agents

        为所有智能体执行动作序列，收集执行结果并更新环境状态。

        对每个智能体:
          1. 解析动作字符串为控制器可接受的参数格式
          2. 通过控制器执行动作（或批量执行探索动作）
          3. 记录成功/失败状态并更新历史
          4. 调用检查器验证任务进度
          5. 特别处理 DropOff 动作：只要一个智能体成功放下人员，
             所有执行 DropOff 的智能体都标记为成功

        参数:
            actions (list[str]): 每个智能体要执行的动作字符串列表
        返回:
            tuple: (格式化后的输入字典字符串, 各智能体动作成功状态列表)
        """
        assert self.initialized, f"Environment not .reset(); can't do .step(.)"

        act_successes, act_texts = [], []

        # 记录每个智能体动作的 error_type（供 barrier 按 agent 传播结构化错误码）
        # 让单个智能体的异常动作不会拖垮整个 barrier step 的其它智能体
        self.per_agent_error_types=[ "" ]*self.num_agents

        # 记录 DropOff 动作的成功情况
        # 在最后需要将所有执行 DropOff 的智能体同步标记为成功
        # NOTE: here we append the successes from the drop action
        #       at the end, we have to assign True to success of ALL agents that did this action if any of these is true
        drop_successes={}

        # --- 逐个智能体执行动作循环 ---
        for agent_idx in range(self.num_agents):
            action=actions[agent_idx]

            # @ coela - added this to handle SendMessage
            # 处理智能体间通信消息，直接标记为成功
            try:
                if "SendMessage" in action:
                    act_success=True
                    error_type=''
                else:
                    # 解析动作字符串为控制器参数
                    action_kwargs=self.parse_action(action, agent_idx)

                    # 探索动作会展开为多个 Move 子步骤，批量执行
                    if "Explore" in action:
                        events=[self.controller.step(**kwargs) for kwargs in action_kwargs]
                        # 取第一个成功的event作为结果（观测内容一致）
                        successes=[int(e['success']) for e in events]
                        if 1 in successes: self.event=events[successes.index(1)]
                        else: self.event=events[-1]

                    else: self.event=self.controller.step(**action_kwargs)

                    act_success=self.event['success']
                    error_type=self.event['error_type']
            except Exception as exc:
                # 单个智能体的畸形/异常动作不能拖垮整个 barrier step：
                # 记录结构化失败并继续执行其它智能体
                act_success=False
                error_type='invalid_action'
                self.event={
                    'success' : False,
                    'global_obs' : None,
                    'local_obs' : None,
                    'visual_obs' : None,
                    'error_type' : error_type,
                    'info' : f"invalid_action: {type(exc).__name__}: {exc}",
                }

            self.per_agent_error_types[agent_idx]=error_type

            self.previous_success[agent_idx]=act_success
            self.step_num[agent_idx]+=1

            # 如果启用了帧保存，保存当前渲染图像
            if self.save_frames: self.save_frame()

            # @bug in original implementation, failure acts don't say previous failures
            # fixed by not resetting if successful
            # 记录失败动作（供后续 LLM 提示使用）
            if not act_success:
                self.agent_failure_acts[self.agent_names[agent_idx]].add(action)
            else:
                # 如果动作成功，从失败记录中移除，避免混淆 LLM
                if action in self.agent_failure_acts[self.agent_names[agent_idx]]:
                    self.agent_failure_acts[self.agent_names[agent_idx]].remove(action)

            # 记录动作历史
            self.action_history[self.agent_names[agent_idx]].append(action)
            self.step_nums_history[self.agent_names[agent_idx]].append(self.step_num[agent_idx])
            self.action_success_history[self.agent_names[agent_idx]].append(act_success)

            # --- 调用检查器验证任务进度 ---
            # 调用检查器执行任务进度验证
            # （观测是最新的，所以检查器能正确判断火灾是否已被扑灭）
            # NOTE: that the this observation ISN'T stale, so checker can properly check for ending fire (once last agent has finished)
            # @here
            self.checker.perform_metric_check(
                action, act_success, self.controller.get_observation(agent_idx)
            )

            # 生成动作执行结果的可读文本
            act_text = self.get_act_text(
                action, act_success, agent_idx, error_type
            )

            # 记录 DropOff 动作的成功状态，用于后续同步
            if "DropOff" in action:
                drop_successes[agent_idx]=act_success

            act_texts.append(act_text)
            act_successes.append(act_success)

        # ====== DropOff 成功同步 ======
        # 如果任一智能体的 DropOff 成功，将所有执行 DropOff 的智能体标记为成功
        if any(drop_successes.values()):
            for idx in drop_successes.keys():
                act=self.action_history[self.agent_names[idx]][-1]

                self.previous_success[idx]=True
                act_successes[idx]=True
                act_texts[idx]=self.get_act_text(act, True, idx, '')
                self.per_agent_error_types[idx]=''

                self.action_success_history[self.agent_names[idx]][-1]=True

                # 移除失败记录
                if act in self.agent_failure_acts[self.agent_names[idx]]:
                    self.agent_failure_acts[self.agent_names[idx]].remove(act)

        # 更新环境状态并返回格式化结果
        self.update_current_state(act_texts)
        return SARBaseEnv.convert_dict_to_string(self.input_dict), act_successes

    def render(self, save_path=None, show=True):
        """
        渲染当前环境状态的可视化图像。

        使用 utils.render 函数绘制所有对象的俯视图。
        支持保存到文件和/或实时显示。

        参数:
            save_path (str, optional): 图像保存路径，若为 None 则不保存
            show (bool): 是否在屏幕上显示渲染结果
        """
        # 渲染函数：将场景中所有对象绘制为图像
        render(self.controller.field.all_objects(expand=True, with_memory=False), save_path=save_path, show=show)

    def _create_path(self, pth):
        """
        创建目录路径（如果尚不存在）。

        参数:
            pth: 要创建的路径对象或路径字符串
        """
        if not os.path.exists(pth):
            os.makedirs(pth)

    def save_frame(self):
        """
        将当前环境状态保存为 PNG 图像文件。

        图像保存在 reset 中设置的渲染路径下，
        文件名格式为 "frame_{step_num}.png"。
        """
        agent_dir_pov = self.render_image_path
        self._create_path(agent_dir_pov)

        pth = agent_dir_pov / f"frame_{self.step_num[0]}.png"
        self.render(save_path=pth, show=False)


if __name__=="__main__":
    env=SAREnv(num_agents=3)
