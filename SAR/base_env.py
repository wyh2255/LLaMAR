# 导入核心引擎模块
import core
# 导入控制器类，用于动作分发
from core import Controller
# 导入类型标注工具
from typing import List, Tuple, Dict
# 探索动作所需的随机、工具和数学模块
# for explore
import random, misc, math

class SARBaseEnv:
    """
    Contains useful functions for SAREnv.
    Avoid clutter in main class

    SAR 基础环境类，提供 SAREnv 所需的公共工具方法。
    包括动作解析、失败文本生成、探索路径规划等，
    将辅助功能从 SAREnv 主类中分离，避免代码臃肿。

    主要功能：
      - 动作字符串解析（parse_action），将 LLM 输出的文本动作转为控制器参数
      - 动作结果文本生成（get_act_text），生成 LLM 可读的执行结果描述
      - 失败文本生成（get_act_failure_text），汇总先前的失败动作
      - 探索路径规划（explore_actions），生成随机探索的移动序列
      - 字典到字符串转换（convert_dict_to_string），格式化状态字典供 LLM 消费
    """

    """
    Controller docs:

    ------------------------------
    .step(action, action_args)
    action_args:
        'NavigateTo' : to_target_id (location),
        'Move' : direction, # Up, Down, Left, Right
        'Carry' : from_target_id (person),
        'DropOff' : from_target_id (person), to_target_id (deposit),
        'StoreSupply' : to_target_id (deposit),
        'UseSupply' : from_target_id (fire),
        'GetSupply' : from_target_id (deposit or reservoir),
        'ClearInventory',
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
    # 探索动作的最大步数（控制每次 Explore 的移动距离）
    MAX_STEPS_FOR_EXPLORE=20
    # 随机种子（必须在初始化前设置）
    seed=None # must initialize seed

    def __init__(self):
        """SARBaseEnv 初始化函数。当前为空，所有初始化逻辑在子类 SAREnv 中完成。"""
        pass

    @staticmethod
    def convert_dict_to_string(input_dict : dict, pre : str="") -> str:
        """
        add new lines for each key value pair
        Example output:
        {Task: bring a tomato, lettuce and bread to the countertop to make a sandwich
        Alice's observation: I see: ['Cabinet_1', ...
        Alice's state: I am at co-ordinates: (-1.00, 0.90, 1.00) and I am holding nothing
        ...
        }

        将输入字典转换为适合 LLM 提示的字符串格式。
        每个键值对占一行，使用换行符分隔。
        可选参数 pre 用于添加缩进前缀。

        参数:
            input_dict (dict): 要转换的输入字典
            pre (str): 每行前的缩进前缀（如制表符）
        返回:
            str: 格式化后的字符串，外层用花括号包裹
        """
        return "{\n" + "\n".join(f"{pre}{k}: {v}, " for k, v in input_dict.items()) + "\n"+f"{pre}}}"

    def get_act_failure_text(self, actions: List[str], agent_idx: int) -> str:
        """
        Get a text describing that the previous actions failed
        actions: List of failed actions, agent_id: int
        actions = [<action_1>, <action_2>, ..., <action_n>]
        Example: Previously, I have tried to <action_1>, <action_2>, ..., <action_n> but was unsuccessful.
        Previously, I have tried to put the Egg_1 in my hand on CounterTop_1, move ahead, rotate left but was unsuccessful.

        生成描述先前动作执行失败的文本，用于 LLM 提示。
        将失败的动作列表拼接成可读的自然语言字符串，
        帮助 LLM 了解哪些操作之前失败过，从而调整策略。

        参数:
            actions (List[str]): 失败的动作列表
            agent_idx (int): 智能体索引
        返回:
            str: 描述失败动作的文本，如 "None" 或 "Previously, I have tried to ... but was unsuccessful"
        """

        """
        MOVEMENT_ACTIONS=['NavigateTo', 'Move']
        CARRY_DROP_ACTIONS=['Carry', 'DropOff']
        SUPPLY_ACTIONS=['StoreSupply', 'UseSupply', 'GetSupply', 'ClearInventory']
        """
        # TODO: how to add two parameters to action

        if len(actions) == 0:
            return "None"
        start_phrase = "Previously, I have tried to "
        end_phrase = ", but was unsuccessful"

        act_texts=[]
        for action in actions:
            act_text=self.parse_action(action, agent_idx, return_dict=False)
            act_texts.append(act_text)

        # merge
        inner_phrase=','.join(act_texts)
        failure_text=start_phrase+inner_phrase+end_phrase
        return failure_text

    def get_act_text(self, action: str, act_success: bool, agent_idx: int, error_type: str = None) -> str:
        """
        Get a text describing what changes the action taken in the previous step

        生成描述上一步执行结果的自然语言文本。
        包含动作描述、成功/失败状态以及可选的错误类型说明。
        该文本将填入 LLM 提示的 "{agent_name}'s previous action" 字段。

        参数:
            action (str): 动作字符串
            act_success (bool): 动作是否成功
            agent_idx (int): 智能体索引
            error_type (str, optional): 错误类型（'not_visible', 'not_interactable', 'restricted_action' 等）
        返回:
            str: 描述动作执行结果的文本
        """

        # --- 组装结果文本 ---
        start_text="I tried to"
        success_text="and was successful."
        unsuccess_text="and was not successful."
        end_text=success_text if act_success else unsuccess_text
        # use fn below
        act_text=self.parse_action(action, agent_idx, return_dict=False)

        # 根据错误类型添加中间描述文本
        if error_type=="not_visible":
            middle_text=" but it wasn't visible "          # 目标不可见
        elif error_type=="not_interactable":
            middle_text=" but I wasn't close enough "       # 距离太远无法交互
        elif error_type=="restricted_action":
            middle_text=" but I was already holding person "  # 已搬运人员，受限动作
        else:
            middle_text=" "

        s=f"{start_text} {act_text}{middle_text}{end_text}"
        return s

    def parse_action(self, action : str, agent_idx : int, return_dict=True):
        """
        parse action string (in format specified in LLM prompt) to format usable to controller
        CAN return list of kwargs (in the case of Explore for instance)
        RETURN: formatted_action

        解析 LLM 输出的动作字符串，转换为控制器可接受的参数格式。
        支持对 "Done"/"Idle" 的自动 fallback 以及 "Explore" 的批量展开。
        可选返回动作参数字典或可读文本。

        解析流程：
          1. "Done"/"Idle" 自动转为 "NoOp()"
          2. 提取动作名称和括号内的参数
          3. 按动作类型分别构造参数字典：
             - 移动类：NavigateTo, Move, Explore
             - 搬运类：Carry, DropOff
             - 供给类：StoreSupply, UseSupply, GetSupply
             - 空操作：ClearInventory, NoOp
          4. 若 return_dict=False，返回可读的文本描述（用于失败/成功日志）

        参数:
            action (str): 动作字符串，如 "NavigateTo(GreatFire)" 或 "UseSupply(GreatFire, Sand)"
            agent_idx (int): 执行该动作的智能体索引
            return_dict (bool): 若为 True 返回动作参数字典，否则返回可读文本
        返回:
            dict 或 str: 动作参数字典或可读的动作描述文本
        """

        # --- "Done"/"Idle" 转为 NoOp ---
        # "Done" 和 "Idle" 转为空操作 NoOp
        if action in ["Done", "Idle"]:
            action="NoOp()"

        # 提取动作名称（括号前部分）
        action_name = action.split("(")[0]
        # 提取括号内的参数部分（可能包含逗号分隔的多个参数）
        # NOTE: action_inner could be two (i.e. "GreatFire, Sand")
        action_inner = action.split("(")[1].split(")")[0]

        # 构建动作参数字典，包含智能体索引和动作名称
        action_dict={}
        action_dict['agent_idx']=agent_idx
        action_dict['action']=action_name

        # 用于生成可读的动作描述文本（在 get_act_failure_text 中使用）
        # such as NavigateTo -> navigate to GreatFire, etc
        # use in get_act_failure_text fn
        act_text=None

        """
        Formatting for actions, required action_args:

        'NavigateTo' : to_target_id (location),
        'Move' : to_target_id (direction), # Up, Down, Left, Right
        'Carry' : from_target_id (person),
        'DropOff' : from_target_id (person), to_target_id (deposit)
        'StoreSupply' : to_target_id (deposit),
        'UseSupply' : to_target_id (fire), supply_type (on fire)
        'GetSupply' : from_target_id (deposit or reservoir), supply_type (deposit)
        'ClearInventory',
        'NoOp',

        (extra) 'Explore'

        Types of error_type(s):
        -restricted_action, not_visible, not_interactable
        """

        # 此函数通过 self.get_id 获取对象 ID，因此需要控制器实例（由 env 提供）
        # NOTE: can only use this function when we have get_id (i.e. called from env, has controller)
        # yet we're putting it in BaseEnv to avoid clutter
        assert hasattr(self, 'get_id'), f"Cannot call this function without having .get_id(.)"

        # ====== 移动类动作 ======
        # Movement actions
        if action_name=="NavigateTo":
            action_dict["to_target_id"]=self.get_id(action_inner) # 目标位置对象 ID
            act_text=f"navigate to {action_inner}"
        if action_name=="Move":
            action_dict["to_target_id"]=action_inner # 移动方向
            act_text=f"move {action_inner}"
        if action_name=="Explore":
            # 调用 explore_actions 生成一系列 Move 动作，利用智能体当前位置
            # we assume that this is called when we have the controller, so we use the position of the agent
            action_dict=self.explore_actions(agent_idx=agent_idx)
            act_text="explore"

        # ====== 搬运/放下类动作 ======
        # Carry Drop actions
        if action_name=="Carry":
            action_dict["from_target_id"]=self.get_id(action_inner) # 被搬运的人员 ID
            act_text=f"carry {action_inner}"
        if action_name=="DropOff":
            # 参数格式："Deposit名称, Person名称"
            deposit_name,person_name=action_inner.split(", ")
            action_dict["to_target_id"]=self.get_id(deposit_name)   # deposit 目标 ID
            action_dict["from_target_id"]=self.get_id(person_name)  # 被放下的人员 ID
            act_text=f"drop off {person_name} at {deposit_name}"

        # ====== 供给类动作 ======
        # supply actions
        if action_name=="StoreSupply":
            action_dict["to_target_id"]=self.get_id(action_inner) # deposit 目标 ID
            act_text=f"store all my supplies at {action_inner}"
        if action_name=="UseSupply":
            # 参数格式："Fire名称, Supply类型（Sand/Water）"
            fire_name,supply_type=action_inner.split(", ")
            action_dict["to_target_id"]=self.get_id(fire_name)    # 目标火灾 ID
            action_dict["supply_type"]=supply_type                # 灭火资源类型
            act_text=f"use {supply_type.lower()} on {fire_name}"
        if action_name=="GetSupply":
            # 通过动作参数中的对象类型名称来区分是从 Deposit 还是 Reservoir 获取
            # NOTE: by convention enforced on procedural generation, the name contains the object type on it (except for agent)
            if "Deposit" in action_inner:
                # 参数格式："Deposit名称, Supply类型"
                deposit_name,supply_type=action_inner.split(", ")
                action_dict["from_target_id"]=self.get_id(deposit_name)  # deposit 来源 ID
                action_dict["supply_type"]=supply_type                   # 资源类型
                act_text=f"get {supply_type.lower()} from {deposit_name}"
            elif "Reservoir" in action_inner:
                action_dict["from_target_id"]=self.get_id(action_inner)  # reservoir 来源 ID
                act_text=f"get supply from {action_inner}"
        # ClearInventory 不需要额外参数
        # no need to do anything for action dict - for both below
        if action_name=="ClearInventory":
            act_text="clear inventory"

        # ====== 空操作 ======
        # no op actions
        if action_name=="NoOp":
            act_text="be idle"

        if return_dict:
            return action_dict
        return act_text

    def explore_actions(self, agent_idx):
        """
        生成探索动作序列。让智能体沿随机方向移动一段距离，
        避免重复或原路返回，并在遇到障碍物时自动调整方向。

        每一步为 Move 动作，除最后一步外均为"惰性步骤"（inert_step=True），
        即不会触发完整的观测更新，以提高效率。

        方向选择策略：
          1. 不允许与上一次探索相同或相反的方向
          2. 不允许零方向（(0,0)）
          3. 丢弃被边界截断超过 75% 的方向
          4. 按随机方向在 x 和 y 轴上交替移动

        参数:
            agent_idx (int): 智能体索引
        返回:
            list[dict]: 一系列 Move 动作参数字典的列表
        """
        # NOTE: This fn is sorta thrown together
        # @here

        # --- 初始化方向历史 ---
        # 初始化方向历史记录，用于避免重复和掉头
        # (a or b) <-> not (nota and notb)
        if not hasattr(self, 'exploration_direction_history'):
            self.dir_history=[(0,0)]

        # ====== 辅助函数定义 ======
        get_rnd_dir=lambda : (random.randint(0,2)-1, random.randint(0,2)-1) # 从 [-1,0,1] x [-1,0,1] 随机采样方向
        abs_sum=lambda l : sum([abs(v) for v in l])     # 向量各分量绝对值之和
        negative=lambda t : tuple([-tt for tt in t])     # 向量取反（相反方向）
        add=lambda t1, t2 : tuple([tt1+tt2 for tt1,tt2 in zip(t1,t2)])  # 向量加法
        mul_scalar=lambda t, s : tuple([tt*s for tt in t])              # 向量数乘
        manhattan=lambda t1, t2: sum([abs(tt1-tt2) for tt1,tt2 in zip(t1,t2)])  # 曼哈顿距离
        steps=lambda rnd_dir : SARBaseEnv.MAX_STEPS_FOR_EXPLORE // abs_sum(rnd_dir)  # 每个方向上的步数
        bound_tuple=lambda t : tuple([misc.clamp(t[0], 0, core.Coordinate.WIDTH), misc.clamp(t[1], 0, core.Coordinate.HEIGHT)])  # 将坐标限制在网格范围内
        agent=self.controller.get('agents', agent_idx)

        def truncated_direction(rnd_dir, minimum_perc):
            """
            检查沿随机方向移动是否会被边界截断（即到达地图边缘）。
            实际移动距离与理论移动距离的比值小于阈值时视为截断。

            参数:
                rnd_dir (tuple): 随机方向向量 (dx, dy)
                minimum_perc (float): 截断判定阈值（如 0.75 表示实际移动不到理论的 75% 则视为截断）
            返回:
                bool: 如果方向被截断返回 True，否则返回 False
            """
            delta_pos=mul_scalar(rnd_dir, steps(rnd_dir))

            curr_pos=agent.get_position()
            next_pos_theory=add(curr_pos,delta_pos)
            next_pos_actual=bound_tuple(next_pos_theory)

            distance_theory=manhattan(curr_pos, next_pos_theory)
            distance_actual=manhattan(curr_pos, next_pos_actual)
            if distance_theory==0:  return True
            assert 0<=distance_actual/distance_theory<=1, f"Something weird happened in truncated_direction, ratio is not [0,1]"

            # 如果实际/理论距离比低于阈值，说明方向被截断
            # if ratio falls below minimum percent, it's 'truncated'
            return  (distance_actual/distance_theory) < minimum_perc

        # ====== 选择随机方向 ======
        # 方向选择不是完全随机的：不允许与上一次相同或相反
        # get 'random' direction
        # not entirely random as we don't allow:
        # previous repetition NOR going in opposite direction as previous
        random_direction=get_rnd_dir()
        previous_dir=self.dir_history[-1]

        # 循环直到找到一个有效方向：
        #   - 不与上次方向相同或相反
        #   - 不是零方向
        #   - 不在该方向上被障碍物/边界截断超过 75%
        while (random_direction in [previous_dir, negative(previous_dir), (0,0)]) or truncated_direction(random_direction, .75):
            random_direction=get_rnd_dir()
        self.dir_history.append(random_direction)

        # ====== 生成移动动作序列 ======
        dx,dy=random_direction
        explore_actions=[]
        for clk in range(steps(random_direction)):
            # 除最后一步外，所有中间步骤标记为 inert（不触发完整观测更新）
            # make all these steps inert

            # 在 x 方向移动
            dx_readable=["Left", "Idle", "Right"][dx+1]
            if dx_readable!="Idle":
                action=f"Move({dx_readable})"
                kwargs=self.parse_action(action, agent_idx=agent_idx)
                kwargs['inert_step']=True
                explore_actions.append(kwargs)


            # 在 y 方向移动
            dy_readable=["Up", "Idle", "Down"][dy+1]
            if dy_readable!="Idle":
                action=f"Move({dy_readable})"
                kwargs=self.parse_action(action, agent_idx=agent_idx)
                kwargs['inert_step']=True
                explore_actions.append(kwargs)

        # 最后一步不做惰性处理，触发观测更新
        explore_actions[-1]['inert_step']=False # make last one non-inert
        return explore_actions

    def stop(self):
        """
        停止环境并释放资源。
        当前实现为空操作，可根据需要扩展关闭控制器等清理逻辑。
        """
        pass

"""
llm interface
feedback

error propagation:
    minimal error feedback, explicitly say environment rules in prompt
        -rules including the amt that is collected from deposits, reservoirs, and dropped into fires
        -(rules about low-level movement) which objects are collidable, and why move would fail
        -grid and bounds - scene boundary (can say 'empty' but that can mean boundary)
        -using appropriate resource for type of fire (sand vs. water) - should we give it fire type? (for now yes)
        -using supply on fire can only fail if wrong type supply used, OR if no flammables were neighbooring or on the agent itself
            -chemical, non-chemical -> sand, water mapping

    -fire having average intensity of None means that it's extinguished
    -since fire is raging on, you might have to collect water multiple times; time is of the essence
        -might make more sense to tame fire before it spreads too much

    -carrying agent:
        -can't do navigation action if grounded
        -inventory is full (can't use AND can't clear)


observation description:

    hardcore certain global variables into the prompt (such as inventory capacity, time from ltom,mtoh)
    inventory:
        -DO: emphasize having person in inventory (and how it's EXCLUSIVE!)
"""

"""
Example of input:

{
Task: ,
Alice's observation:
    Directly around me, I can see:
    {
    Left: ['Flammable of fire (intensity None): GreatFire'],
    Up: ['Flammable of fire (intensity None): GreatFire'],
    Center: ['Flammable of fire (intensity None): GreatFire'],
    Down: ['Flammable of fire (intensity None): GreatFire'],
    Right: ['Flammable of fire (intensity None): GreatFire'],
    },
    Globally, I can see: ['CaldorFire with average intensity of Low of Chemical type', 'CaldorFire_Region_1 with an intensity of None of Chemical type', 'CaldorFire_Region_2 with an intensity of None of Chemical type', 'CaldorFire_Region_3 with an intensity of None of Chemical type', 'GreatFire with average intensity of Low of Non-chemical type', 'GreatFire_Region_1 with an intensity of None of Non-chemical type', 'GreatFire_Region_2 with an intensity of None of Non-chemical type', 'GreatFire_Region_3 with an intensity of None of Non-chemical type', 'ReservoirUtah containing Sand', 'ReservoirYork containing Water', "DepositFacility containing {'Sand': 0, 'Water': 0, 'Person': 0}"],
    Names: ['CaldorFire', 'CaldorFire_Region_1', 'CaldorFire_Region_2', 'CaldorFire_Region_3', 'GreatFire', 'GreatFire_Region_1', 'GreatFire_Region_2', 'GreatFire_Region_3', 'ReservoirUtah', 'ReservoirYork', 'DepositFacility'],
Alice's state: I am at co-ordinates: (22, 18) and I am holding {'Sand': 0, 'Water': 1, 'Person': 0}.,
Alice's previous observation: ['CaldorFire', 'CaldorFire_Region_1', 'CaldorFire_Region_2', 'CaldorFire_Region_3', 'GreatFire', 'GreatFire_Region_1', 'GreatFire_Region_2', 'GreatFire_Region_3', 'ReservoirUtah', 'ReservoirYork', 'DepositFacility'],
Alice's previous action: I tried to use water on GreatFire and was successful.,
Alice's previous failures: Previously, I have tried to use water on GreatFire, but was unsuccessful,
Bob's observation:
    Directly around me, I can see:
    {
    Left: ['Flammable of fire (intensity Low): CaldorFire'],
    Up: ['Flammable of fire (intensity Low): CaldorFire'],
    Center: ['Flammable of fire (intensity None): CaldorFire'],
    Down: ['Flammable of fire (intensity None): CaldorFire'],
    Right: ['Flammable of fire (intensity None): CaldorFire'],
    },
    Globally, I can see: ['CaldorFire with average intensity of Low of Chemical type', 'CaldorFire_Region_1 with an intensity of None of Chemical type', 'CaldorFire_Region_2 with an intensity of None of Chemical type', 'CaldorFire_Region_3 with an intensity of None of Chemical type', 'GreatFire with average intensity of Low of Non-chemical type', 'GreatFire_Region_1 with an intensity of None of Non-chemical type', 'GreatFire_Region_2 with an intensity of None of Non-chemical type', 'GreatFire_Region_3 with an intensity of None of Non-chemical type', 'ReservoirUtah containing Sand', 'ReservoirYork containing Water', "DepositFacility containing {'Sand': 0, 'Water': 0, 'Person': 0}"],
    Names: ['CaldorFire', 'CaldorFire_Region_1', 'CaldorFire_Region_2', 'CaldorFire_Region_3', 'GreatFire', 'GreatFire_Region_1', 'GreatFire_Region_2', 'GreatFire_Region_3', 'ReservoirUtah', 'ReservoirYork', 'DepositFacility'],
Bob's state: I am at co-ordinates: (4, 4) and I am holding {'Sand': 1, 'Water': 0, 'Person': 0}.,
Bob's previous observation: ['CaldorFire', 'CaldorFire_Region_1', 'CaldorFire_Region_2', 'CaldorFire_Region_3', 'GreatFire', 'GreatFire_Region_1', 'GreatFire_Region_2', 'GreatFire_Region_3', 'ReservoirUtah', 'ReservoirYork', 'DepositFacility'],
Bob's previous action: I tried to use sand on CaldorFire and was successful.,
Bob's previous failures: Previously, I have tried to use sand on CaldorFire, but was unsuccessful,
Robots' subtasks: [],
Robots' combined memory: [],
Robots' open subtasks: [],
Robots' completed subtasks: [],
}

"""
