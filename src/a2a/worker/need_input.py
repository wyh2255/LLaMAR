"""NeedInputError — Worker agent 需要向 Coordinator 请求输入时抛出。"""


class NeedInputError(Exception):
    """Worker agent 需要向 Coordinator 请求输入时抛出。

    捕获后应调用 TaskUpdater.requires_input() 设置 A2A INPUT_REQUIRED 状态，
    然后从 AgentAdapter.execute() 返回让出控制权。
    """

    def __init__(self, question: str) -> None:
        self.question = question
        super().__init__(question)
