"""
子任务完成度检查器基类 (BaseChecker)
=====================================
定义通用的子任务完成检查、覆盖率追踪和成功率判断框架。
所有场景特定的检查器（如 Checker）都继承自此类。

核心功能:
  - _check_subtask():  检查单个动作是否完成对应子任务
  - check_coverage():  追踪哪些对象已被覆盖
  - check_success():   判断所有子任务是否已完成
"""

class BaseChecker:
    """
    子任务检查器基类。

    维护两个核心列表:
      - subtasks / subtasks_completed: 所有子任务 和 已完成子任务
      - coverage / coverage_completed:  所有待覆盖对象 和 已覆盖对象

    子类需要:
      1. 在 __init__ 中传入 subtasks 和 coverage
      2. 实现 callback() 方法处理每步结束后的回调逻辑
    """
    def __init__(
            self,
            subtasks,
            coverage,
            ) -> None:

        # NOTE: 当前环境中所有子任务都是无条件触发的（不依赖于其他任务完成）
        # NOTE: All subtasks are non-conditional for this environment
        self.subtasks = subtasks
        # objects or receptacles — 需要检查的对象或容器列表
        self.coverage = coverage

        self.subtasks_completed = []    # 已完成的子任务列表
        self.coverage_completed = []    # 已完成覆盖检查的对象列表

        self.event=None                # 最近一次观测事件的缓存

         # 子类中还可访问 self.available_object_names（由 Checker.initialize 设置）
         # we also have self.available_object_names from the child class

    @property
    def subtasks_completed_numerated(self):
        """
        返回带编号的已完成子任务列表（属性形式）。
        当前实现与 subtasks_completed 相同，因为不进行编号区分。
        # @coela 如果将来需要按对象编号区分子任务，可在此扩展
        """
        # NOTE: 当前与 subtasks_completed 相同，因为我们不对子任务编号
        # NOTE: This is the same as subtasks completed b/c we don't numerate
        return self.subtasks_completed

    def split_action(self, action: str):
        """
        将动作字符串拆分为动作名称和参数。
        例如: "UseSupply(Fire_1, water)" → ("UseSupply", "Fire_1, water")
              若参数包含逗号，则转换为元组: ("Carry", ("Person_1", "Agent_2"))

        参数:
            action (str): 完整动作字符串，格式为 "ActionName(arg1, arg2)"

        返回:
            tuple: (action_name, inner)
                - action_name: 动作名称字符串
                - inner: 参数（字符串或元组，当含多个参数时）
        """
        act = action.split("(")[0]
        inner = action.split("(")[1].split(")")[0]
        if "," in inner:
            inner = tuple(inner.split(", "))
        return act, inner

    def unsplit_action(self, action: str, inner):
        """
        split_action 的逆操作：将动作名称和参数重新拼接为完整动作字符串。

        参数:
            action (str): 动作名称
            inner (str 或 tuple): 参数，若为元组则自动用 ", " 连接

        返回:
            str: 完整动作字符串，如 "UseSupply(Fire_1, water)"
        """
        if isinstance(inner, tuple):
            return f"{action}({', '.join(inner)})"
        return f"{action}({inner})"

    def give_credit_for_navigate(self, action, success):
        """
        当某个非导航动作成功时，自动为对应的 NavigateTo 子任务计分。
        因为如果智能体成功执行了 UseSupply(Fire_1)，意味着它已经到达了 Fire_1 附近。
        这样可以追踪到达事件，即使没有显式执行 NavigateTo 指令。

        参数:
            action (str): 当前执行的动作字符串
            success (bool): 动作是否成功
        """
        act, inner = self.split_action(action)
        if act not in ["NavigateTo"]:
            navigate_action = f"NavigateTo({object})"
            self._check_subtask(navigate_action, success)

    def _check_subtask(
        self,
        action,
        success,
    ):
        """
        核心子任务检查逻辑。
        根据当前动作及其成功/失败状态，判断是否应标记某个子任务为已完成。

        检查流程:
          1. 跳过空闲/消息类动作（如 Done, Idle, SendMessage）
          2. 去除对象名中的区域后缀（如 "Fire_1_Region_3" → "Fire_1"）
          3. 若动作在子任务列表中且尚未完成且执行成功，则标记为已完成
          4. 自动为导航动作计分（give_credit_for_navigate）

        参数:
            action (str):  当前执行的动作字符串
            success (bool): 动作是否成功执行
        """
        # idle actions — 空闲/消息类动作不触发任何检查
        if action in ["Done", "Idle"]:
            return None
        if "SendMessage" in action:
            return None

        # NOTE: 去除对象名中的区域后缀（火灾对象通常带有 _Region_X 后缀）
        # NOTE: replace the inner part w/ non-region (for fire)
        deregionize = lambda s : s[:s.index('_Region_')] if '_Region_' in s else s
        act, inner = self.split_action(action)
        if isinstance(inner, tuple):
            inner=tuple([deregionize(p) for p in inner])
        else: inner=deregionize(inner)
        # modified action after de-regionizing — 去区域化后的动作字符串
        action = self.unsplit_action(act, inner)

        # 如果该动作在子任务列表中、尚未完成、且执行成功，则标记为已完成
        if (
            action in self.subtasks
            and action not in self.subtasks_completed
            and success
        ):
            self.subtasks_completed.append(action)

            # 自动为对应的导航子任务（NavigateTo）计分
            # 因为智能体能够执行动作意味着它已在目标附近
            # Give credit for NavigateTo(object) if action(object) is successful
            # Since this means the agent just happened to already be close enough
            self.give_credit_for_navigate(action, success)

    def callback(self):
        """
        回调函数，在每个多智能体步骤的最后执行。
        在基类中为抽象方法，由子类实现具体的场景特定逻辑。
        """
        raise NotImplementedError

    def get_transport_rate(self):
        """
        获取运输进度（已完成子任务 / 总子任务数）。
        表示整体任务完成比例。

        返回:
            float: 0.0 ~ 1.0 之间的比例值
        """
        return len(self.subtasks_completed) / len(self.subtasks)

    def check_coverage(self, action: str):
        """
        检查当前动作涉及的对象是否在覆盖范围内（即是否已被探索/访问）。
        如果对象在 coverage 列表中且尚未被覆盖，则将其加入 coverage_completed。

        参数:
            action (str): 当前执行的动作字符串
        """
        for obj in self.coverage:
            if (obj in action) and (obj not in self.coverage_completed):
                self.coverage_completed.append(obj)

    def perform_metric_check(self, action, success, observation_dct):
        """
        执行所有指标检查的总入口。
        在一次动作完成后，同时执行子任务检查和覆盖率检查，并缓存观测信息。

        参数:
            action (str):        当前执行的动作字符串
            success (bool):      动作是否成功
            observation_dct (dict): 当前时间步的完整观测字典
        """
        self._check_subtask(action, success)
        self.check_coverage(action)

        # 执行子类定义的回调逻辑（检查火灾熄灭、人员发现等）
        self.event=observation_dct
        self.callback()

    def get_coverage(self):
        """
        获取场景覆盖率（已覆盖对象 / 总需覆盖对象）。
        表示场景中被探索或交互过的对象的比例。

        返回:
            float: 0.0 ~ 1.0 之间的比例值
        """
        return len(self.coverage_completed) / len(self.coverage)

    def check_success(self):
        """
        判断任务是否全部完成：所有子任务均已被标记为已完成。

        返回:
            bool: 所有子任务完成时返回 True，否则返回 False
        """
        return len(self.subtasks_completed) == len(self.subtasks)
