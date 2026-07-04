"""AgentLang — LangGraph 实现的 agent 内核，复用现有 A2A 传输层与 SAR 工具。

与 src/Agent (自定义 ReAct) 功能对齐：
- core_agent.ReActAgent 对应 Agent.run() 循环（LLM+工具+摘要+step_callback+取消+token 记账）
- llm.build_llm 对应 LLMClient（ChatOpenAI/ChatAnthropic）
- tools.adapter.LangGraphToolAdapter 把现有 Tool 子类（SAR/bash/file/skill/mcp）零改写接入
- coordinator.supervisor 对应 RouterAgent/CoordinatorAgentExecutor 编排
"""
