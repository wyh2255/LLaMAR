"""
场景初始化器基类 (BaseSceneInitializer)
========================================
负责验证场景参数完整性、生成任务描述文本、以及创建 Controller 实例。
所有具体的场景定义文件 (scene_N.py) 都继承自此类。
"""

import copy
from core import Controller, Field
from misc import join_conjunction

class BaseSceneInitializer:
    """
    场景初始化器基类。
    通过 check_proper() 验证场景参数结构的完整性，通过 preinit() 创建 Controller 实例。
    子类只需定义 self.params, self.name, self.task_timeout 三个属性即可使用。
    """
    def __init__(self) -> None:
        """构造函数：执行参数完整性检查，并记录最大支持智能体数"""
        self.check_proper()
        self.max_num_agents=len(self.params['agents'])

    def get_task(self):
        """
        根据场景参数自动生成自然语言任务描述。
        遍历火灾列表和人员列表，拼接成一段完整的中/英文任务指令。
        """
        # --- 将火灾类型（如 'A', 'B'）映射为可读的名称（如 'ordinary', 'flammable'） ---
        map_tp=lambda tp : Field.READABLE_TYPE_MAPPER_FIRE[tp.upper()].lower()
        fire_names=[f"{map_tp(arg.tp)} fire {arg.name}" for arg in self.params['fires']]
        person_names=[arg.name for arg in self.params['persons']]
        # v1 - 强调找到人员是最终目标（不会再有更多人员）
        # v1 - emphasize that finding that person is final (no more)
        task=f"Using the appropriate resources, extinguish all the fires: {join_conjunction(fire_names, 'and')}. Also, explore to find the following lost people (and no more): {join_conjunction(person_names, 'and')}, carry them, and drop them in a deposit."
        return task

    def check_proper(self):
        """ 检查初始化参数是否正确且完整 """
        # 确保 self.params 已存在
        assert hasattr(self, 'params'), f"Parameters have not been created."
        # 确保 self.name 已存在
        assert hasattr(self, 'name'), f"Name for initialization not provided."
        # 确保 self.task_timeout 已存在
        assert hasattr(self, 'task_timeout'), f"Task timeout not provided."

        # --- 命名约定检查：每个对象的 name 必须包含其类名（agents 除外） ---
        # naming convention: Name must contain class type (except agents)
        for poi,l in self.params.items():
            if poi in ['grid_size','agents']: continue
            o_type=poi[:-len('s')].capitalize()
            for i,arg in enumerate(l):
                assert o_type in arg.name, f"Object of type {o_type} in initialization does not contain '{o_type}' in name {arg.name}"

        # --- 资源类型覆盖检查：每种火灾类型必须有对应的水库类型 ---
        fire_type_spectrum=set([arg.tp for arg in self.params['fires']])
        reservoir_type_spectrum=set([arg.tp for arg in self.params['reservoirs']])
        assert fire_type_spectrum.issubset(reservoir_type_spectrum), f"Not the fire types ({fire_type_spectrum}) must be represented in the reservoir types ({reservoir_type_spectrum})"

        # --- 必需键存在性检查：所有关键类别都不能为空 ---
        keys=['reservoirs', 'deposits', 'fires', 'persons', 'agents']
        for k in keys: assert len(self.params.get(k,[]))>0, f"In params, key {k} has no arguments."

        # --- 对象名保留字检查：不能使用类名作为对象名（保留给泛型使用） ---
        for k in keys[:-1]: # except for agents, they're named separately
            class_type=k[:-1].capitalize()
            for v in self.params[k]: assert (v.name!=class_type), f"Cannot name object with same name as class type ({class_type}), that name is reserved"

        # --- grid_size 格式检查：必须是长度为 3 的正整元组 (x,y,z) ---
        grid_size=self.params.get('grid_size',[])
        assert isinstance(grid_size, tuple) and len(grid_size)==3, f"Grid size parameter is not tuple, or is wrong size (must have x,y,z)."
        # should be positive
        assert grid_size[0]>0 and grid_size[1]>0, f"Grid size has non-positive entries."


    def preinit(self, num_agents, agent_names, seed):
        """
        初始化场景控制器并返回参数副本。
        根据实际智能体数量裁剪配置，为每个智能体分配名称，最后创建 Controller。

        参数:
            num_agents (int): 实际使用的智能体数量
            agent_names (list): 智能体名称列表
            seed (int): 随机种子

        返回:
            tuple: (controller, pg_params)
                - controller: Controller 实例，用于驱动场景运行
                - pg_params: 处理后的参数字典（供后续可行性动作映射使用）
        """
        # --- 智能体数量合法性检查 ---
        assert 1<=num_agents<=self.max_num_agents, f"For the {self.name} initialization min: 1 and max: {self.max_num_agents} are supported not {num_agents}"
        pg_params=copy.deepcopy(self.params)
        pg_params['agents']=pg_params['agents'][:num_agents]
        # name agents — 为每个智能体分配传入的名称
        for i,args in enumerate(pg_params.get('agents',[])):
            args.name=agent_names[i]
            assert args.name != 'Agent', f"Cannot name agent 'Agent'"

        # ---- 使用处理后的参数初始化 Controller ----
        controller=Controller(procedural_generation_parameters=pg_params, seed=seed)


        # ---- 为可行性动作映射追加附加信息 ----
        # This is added as extra information (used in feasable action mapping)
        pg_params['available_object_names']=controller.all_names

        # ---- 将资源类型名转换为可读格式（如 'A' → 'Ordinary'） ----
        for poi,l in pg_params.items():
            for i,arg in enumerate(l):
                if hasattr(arg, 'tp'):
                    # TODO: 有点取巧地使用了 Field 类的方法
                    # TODO: a bit wonky to use Field class
                    arg.tp=Field.READABLE_TYPE_MAPPER_RESOURCE[arg.tp.upper()].capitalize()

        return controller, pg_params
