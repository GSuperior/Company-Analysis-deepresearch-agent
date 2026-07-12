"""cr_agent: 自研公司深度调研智能体（Custom Research Agent）。

设计理念：让 LLM 做语义，让 controller 做机制。
- LLM 只负责：搜什么、读什么、写什么内容、引用哪张卡片
- Controller 负责：分配 ID、校验格式、计数预算、聚合文件、替换引用、生成 TOC

5 角色流水线：scout → planner → researcher(×N) → writer(×N sections) → reviewer → render(内置)
所有数据均来自真实 web_search/web_fetch，无模拟数据。
"""
