from Scenes.base_checker import BaseChecker
from collections import Counter

class Checker(BaseChecker):
    """
    场景任务完成度检查器。
    负责根据场景参数自动生成子任务列表，并在每一步执行后回调检测任务完成状态。
    """
    def __init__(self, params : dict):
        # 调用 initialize 自动从场景参数生成子任务和覆盖范围
        subtasks,coverage=self.initialize(params)
        super().__init__(
                subtasks,
                coverage
                )

    def initialize(self, params):
        """
        使用控制器初始化参数自动创建子任务列表和覆盖范围。

        遍历场景中的火灾、人员、仓库等对象，为每个可交互对象生成对应的子任务。
        同时收集所有需要检查的对象名称形成覆盖范围。

        参数:
            params (dict): 场景初始化参数字典
        返回:
            subtasks (list): 子任务字符串列表
            coverage (list): 需要覆盖检查的对象名称列表
        """

        subtasks=[]       # 子任务列表
        coverage=[]       # 需覆盖检查的对象名称列表

        # 将所有对象名称提供给基类检查器，用于正确统计 NavigateTo 任务
        # give all objects to base checker class in order to properly account for navigate to
        self.available_object_names=params.pop('available_object_names')

        # 建立"供给类型 -> 水库名称"的映射，检查是否存在类型歧义
        # Check for ambiguity - raise error if so
        supply_to_reservoir={}
        for arg in params["reservoirs"]:
            if supply_to_reservoir.get(arg.tp,None) is not None:
                print("Cannot yet have >1 supply of the same type.")
                raise NotImplementedError
            supply_to_reservoir[arg.tp]=arg.name

        # ----------------------------------------------------------
        # 遍历所有场景对象，收集需要检查覆盖的对象名称（排除 reservoirs 和 agents）
        for poi,l in params.items():
            # don't add agents, see about reservoirs below
            if poi in ['reservoirs', 'agents']: continue
            names=list(map(lambda a : a.name, l))
            coverage.extend(names)

        # 只添加与火灾类型对应的水库（即实际需要使用的供给源）
        # add only reservoirs that have corresponding action
        necessary_types=list(set([a.tp for a in params["fires"]]))
        coverage.extend([supply_to_reservoir[tp] for tp in necessary_types])
        # 去重，防止重复计数
        # avoid double-counting - we didn't, just making sure though
        coverage=list(set(coverage))

        # ---------------------- subtasks 子任务创建 ---------------------------
        # TODO (as needed): implement this w/ multiple deposits
        #       we'll need to add generics to the checker function
        #       (perhaps do auto credit assignment for navigateto(deposit) when doing dropoff?
        # 当前仅支持单个 Deposit，多个 Deposit 会导致任务模糊
        if len(params['deposits'])>1:
            print("Cannot yet use checker w/ more than 1 deposit (ambiguity).")
            raise NotImplementedError
        deposit_singleton=params['deposits'][0]

        # --- 火灾相关子任务 ---
        for arg in params["fires"]:
            subtasks.append(f"NavigateTo({arg.name})")     # 导航到火灾位置
            subtasks.append(f"UseSupply({arg.name}, {arg.tp})")  # 使用对应类型的灭火资源
            # 单独检查火灾是否已被完全扑灭（在 callback 中独立检查）
            subtasks.append(f"EndFire({arg.name})")

        # --- 人员相关子任务 ---
        for arg in params["persons"]:
            subtasks.append(f"NavigateTo({arg.name})")     # 导航到人员位置
            subtasks.append(f"Carry({arg.name})")           # 搬运人员
            # generic deposit (doesn't matter)
            subtasks.append(f"DropOff({deposit_singleton.name}, {arg.name})")  # 将人员送到 Deposit
            subtasks.append(f"NavigateTo({deposit_singleton.name})")            # 导航到 Deposit
            # 在 callback 中独立检查人员是否已被发现
            subtasks.append(f"Spot({arg.name})")

        # --- deposit ---
        # 无需添加子任务，使用 Deposit 不是强制性的

        # --- reservoir ---
        # 根据火灾所需的供给类型，添加获取供给和导航到水库的子任务
        for arg in params["fires"]:
            reservoir_name=supply_to_reservoir[arg.tp]
            subtasks.append(f"GetSupply({reservoir_name})")
            subtasks.append(f"NavigateTo({reservoir_name})")
        # ----------------------------------------------------------

        # 去重，避免子任务重复
        subtasks=list(set(subtasks))
        # print("Checking subtasks:", subtasks)
        # print("Checking coverage:", coverage)

        return subtasks,coverage

    def callback(self):
        """
        在每个多智能体步骤结束时调用，检查火灾是否已被扑灭以及人员是否已被发现。
        如果条件满足，将对应的子任务标记为已完成。

        检查逻辑:
          - Fire 类型: 若 average_intensity 为 'None'，表示火已扑灭
          - Person 类型: 若 spotted 为 True，表示人员已被发现
        """
        for d in self.event.get('global_obs',None):
            # 检查火灾是否已扑灭
            if d['type']=='Fire':
                subtask=f"EndFire({d['name']})"
                average_intensity=d.get('average_intensity', '')
                # 火势已熄灭且尚未标记完成
                # fire has subsided & not already completed
                if average_intensity=='None' and (subtask not in self.subtasks_completed):
                    self.subtasks_completed.append(subtask)

            # 检查人员是否已被发现
            elif d['type']=='Person':
                subtask=f"Spot({d['name']})"
                spotted=d.get('spotted', False)
                if spotted and (subtask not in self.subtasks_completed):
                    self.subtasks_completed.append(subtask)
