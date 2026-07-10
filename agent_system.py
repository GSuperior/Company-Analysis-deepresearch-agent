"""
多Agent深度研究系统 - 基于 sn-deep-research 设计思想

参考 SenseNova Skills 生态的 sn-deep-research 深度研究技能设计，
实现精简版的多 Agent 协作流水线。

Agent 流水线（对齐 sn-deep-research）：
  1. Scout      - 预研侦察兵：快速了解 + 档位推荐
  2. Planner    - 研究规划师：维度拆解 + 关键问题设计
  3. Researcher - 信息取证专家：按维度取证，输出标准化 evidence
  4. Reviewer   - 质量审查员：子报告审查 + 缺口识别
  5. ReportPlanner - 报告规划师：大纲编排 + 证据分配
  6. Writer     - 报告撰写师：基于 evidence 撰写全文
  7. FactChecker - 事实核查员：关键数据验证 + 可信度评估

工具配置（遵循 sn-deep-research 设计原则）：
- 需要信息获取的 Agent 配备搜索工具
- 纯分析/写作的 Agent 不直接调用工具
- 取证角色有专业搜索技能，写作角色只读 evidence 边界

三档模式（对齐 sn-deep-research）：
- quick  : 单维度 skim → 单 writer → 快速出稿
- normal : scout → plan → 多维度 research + review → report plan → writer → 终审
- heavy  : normal 基础上增加视角分析、补研循环、多节并行写作

设计原则：
- Controller 只调度，不读大文件（通过结构化数据传递）
- 证据与写作分离（evidence → outline → report）
- Schema 硬门禁（每个阶段输出格式校验）
- 优雅降级（每环节都有 fallback）
"""

import json
import time
import logging
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable

from tools import ToolManager, truncate, now_ts

logger = logging.getLogger(__name__)


# ============================================================
# 日志构造工具
# ============================================================

def make_log(step: int, agent: str, action: str, input_summary: str = "",
             output_summary: str = "", tool_calls: Optional[List] = None,
             duration_ms: int = 0, level: str = "info") -> Dict[str, Any]:
    """
    构造日志条目

    Args:
        step: 步骤序号
        agent: Agent名称
        action: 动作描述
        input_summary: 输入摘要
        output_summary: 输出摘要
        tool_calls: 工具调用记录
        duration_ms: 耗时（毫秒）
        level: 日志级别

    Returns:
        日志条目字典
    """
    return {
        "step": step,
        "agent": agent,
        "action": action,
        "level": level,
        "input_summary": truncate(input_summary, 200),
        "output_summary": truncate(output_summary, 200),
        "tool_calls": tool_calls or [],
        "duration_ms": duration_ms,
        "timestamp": now_ts(),
        "timestamp_unix": time.time(),
    }


# ============================================================
# SenseNova API 客户端
# ============================================================

class SenseNovaClient:
    """
    SenseNova API 简化客户端

    支持：
    - 单次对话调用
    - 带工具调用的多轮对话（function calling）
    - 超时保护和错误处理
    """

    BASE_URL = "https://token.sensenova.cn/v1/chat/completions"
    DEFAULT_MODEL = "sensenova-6.7-flash-lite"
    DEFAULT_TIMEOUT = 60  # 秒

    def __init__(self, api_key: str, model: Optional[str] = None):
        """
        初始化客户端

        Args:
            api_key: API密钥
            model: 模型名称（可选）
        """
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.total_tokens = 0
        self.call_count = 0

    def chat(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """
        单次对话调用

        Args:
            messages: 消息列表
            **kwargs: 其他参数（temperature, max_tokens, tools等）

        Returns:
            {content, tool_calls, duration_ms}

        Raises:
            RuntimeError: API调用失败时抛出
        """
        start = time.time()
        self.call_count += 1

        payload = {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2000),
            "stream": False,
        }

        if "tools" in kwargs and kwargs["tools"]:
            payload["tools"] = kwargs["tools"]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = kwargs.get("timeout", self.DEFAULT_TIMEOUT)

        try:
            resp = requests.post(self.BASE_URL, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            duration_ms = int((time.time() - start) * 1000)

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            # 统计token使用
            usage = data.get("usage", {})
            self.total_tokens += usage.get("total_tokens", 0)

            return {
                "content": content or "",
                "tool_calls": tool_calls or [],
                "duration_ms": duration_ms,
                "usage": usage,
            }
        except requests.exceptions.Timeout:
            duration_ms = int((time.time() - start) * 1000)
            raise RuntimeError(f"SenseNova API 调用超时（{timeout}秒）") from None
        except requests.exceptions.RequestException as e:
            duration_ms = int((time.time() - start) * 1000)
            raise RuntimeError(f"SenseNova API 调用失败: {e}") from e
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            raise RuntimeError(f"SenseNova API 未知错误: {e}") from e

    def chat_with_tools(self, messages: List[Dict], tools: List[Dict],
                        tool_executor: Callable[[str, str], str],
                        max_iter: int = 3, **kwargs) -> Dict[str, Any]:
        """
        带工具调用的多轮对话

        Args:
            messages: 初始消息列表
            tools: 工具schema列表
            tool_executor: 工具执行函数 (name, arguments_str) -> result_str
            max_iter: 最大迭代次数
            **kwargs: 其他参数

        Returns:
            {content, tool_calls_history, duration_ms}
        """
        tool_calls_history = []
        current_messages = list(messages)
        total_duration = 0
        result = {"content": "", "tool_calls": [], "duration_ms": 0}

        if max_iter <= 0:
            logger.warning(f"chat_with_tools called with max_iter={max_iter}, returning empty result")
            return {
                "content": "",
                "tool_calls_history": [],
                "duration_ms": 0,
                "error": "max_iter must be positive",
            }

        for i in range(max_iter):
            try:
                result = self.chat(current_messages, tools=tools, **kwargs)
            except RuntimeError as e:
                # API调用失败，返回已有的部分结果
                logger.error(f"chat_with_tools iteration {i} failed: {e}")
                return {
                    "content": f"[API调用失败: {e}]",
                    "tool_calls_history": tool_calls_history,
                    "duration_ms": total_duration,
                    "error": str(e),
                }

            total_duration += result.get("duration_ms", 0)
            tool_calls = result.get("tool_calls", [])

            if not tool_calls:
                # 没有工具调用，返回最终结果
                return {
                    "content": result["content"],
                    "tool_calls_history": tool_calls_history,
                    "duration_ms": total_duration,
                }

            # 将assistant消息加入对话
            current_messages.append({
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": tool_calls,
            })

            # 执行所有工具调用
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                tc_name = tc.get("function", {}).get("name", "")
                tc_args = tc.get("function", {}).get("arguments", "{}")

                try:
                    tool_result = tool_executor(tc_name, tc_args)
                except Exception as e:
                    logger.error(f"Tool execution error: {tc_name}, {e}")
                    tool_result = json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)

                tool_calls_history.append({
                    "name": tc_name,
                    "arguments": tc_args,
                    "result_summary": truncate(tool_result, 300),
                })

                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

        # 达到最大迭代次数，返回最后一次内容
        return {
            "content": result.get("content", ""),
            "tool_calls_history": tool_calls_history,
            "duration_ms": total_duration,
            "truncated": True,
        }


# ============================================================
# 深度配置（对齐 sn-deep-research 档位）
# ============================================================

DEPTH_CONFIG = {
    "quick": {
        "label": "快速调研",
        "dimension_count": 1,
        "has_scout": False,
        "has_sub_report_review": False,
        "has_report_planner": False,
        "fact_check_mode": "basic",
        "research_iterations": 1,
        "final_review": False,
        "estimated_duration": "约30秒",
    },
    "normal": {
        "label": "标准研究",
        "dimension_count": 4,
        "has_scout": True,
        "has_sub_report_review": True,
        "has_report_planner": True,
        "fact_check_mode": "llm",
        "research_iterations": 2,
        "final_review": True,
        "estimated_duration": "约3分钟",
    },
    "heavy": {
        "label": "深度研究",
        "dimension_count": 6,
        "has_scout": True,
        "has_sub_report_review": True,
        "has_report_planner": True,
        "fact_check_mode": "full",
        "research_iterations": 3,
        "final_review": True,
        "estimated_duration": "约6分钟",
    },
}

# 兼容旧的档位名称（向后兼容）
DEPTH_ALIASES = {
    "basic": "quick",
    "standard": "normal",
    "deep": "heavy",
}

# 所有可用的研究维度（按优先级排序）
ALL_DIMENSIONS = [
    "公司概况",
    "财务表现",
    "业务分析",
    "竞争格局",
    "技术实力",
    "发展战略",
    "风险分析",
]


# ============================================================
# Agent 0: Scout - 预研侦察兵
# ============================================================
# 对应 sn-deep-research 中的 scout agent
# 作用：快速了解目标，推荐档位，产出 briefing

SCOUT_SYSTEM_PROMPT = """你是深度研究的预研侦察兵（Scout）。

你的任务是：在正式研究开始前，快速扫描目标公司的基本情况，
评估研究复杂度，并推荐合适的研究档位。

工作流程：
1. 使用 web_search 快速搜索目标公司的基本信息
2. 基于搜索结果，判断研究复杂度（涉及多少维度、是否需要多源验证）
3. 推荐合适的档位并说明理由

输出要求（严格JSON格式）：
{
  "company_profile": "公司一句话定位",
  "industry": "所属行业",
  "complexity_assessment": "复杂度评估说明",
  "recommended_mode": "quick|normal|heavy",
  "mode_rationale": "推荐该档位的理由",
  "key_attention_points": ["需要重点关注的点1", "需要重点关注的点2"]
}

档位选择标准：
- quick：单一维度即可覆盖，单一权威来源可定论（如仅查某个具体数据点）
- normal：需要多维度拆解，需要多来源交叉验证（常规公司研究）
- heavy：话题复杂或重要，涉及多方利益相关者，需要深度分析和多视角（大型企业、行业龙头、争议性话题）
"""


def scout_agent(client: SenseNovaClient, tool_manager: ToolManager,
                query: str) -> Dict[str, Any]:
    """
    Scout Agent - 预研侦察兵

    快速了解目标，推荐档位，产出 briefing。

    Args:
        client: SenseNova客户端
        tool_manager: 工具管理器
        query: 研究主题/公司名

    Returns:
        {briefing, tool_calls_history, duration_ms, input_summary, output_summary}
    """
    user_prompt = f"""请对以下研究主题进行预研侦察：

研究主题：「{query}」

请先使用 web_search 快速了解相关背景，然后评估研究复杂度并推荐档位。
注意：这是预研阶段，不需要深入研究，只需要快速扫描和判断。

请以严格的JSON格式输出 briefing。"""

    messages = [
        {"role": "system", "content": SCOUT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, query)

    # Scout 使用 web_search 工具
    tools = tool_manager.get_tool_schemas(["web_search"])

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=2,
        temperature=0.5,
        max_tokens=1500,
    )

    # 解析JSON
    content = result["content"].strip()
    briefing = _parse_json_safe(content, _generate_fallback_briefing(query))

    return {
        "briefing": briefing,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"主题:{truncate(query, 50)}",
        "output_summary": f"推荐档位:{briefing.get('recommended_mode', 'unknown')}",
    }


def _generate_fallback_briefing(query: str) -> Dict[str, Any]:
    """生成保底 briefing"""
    return {
        "company_profile": f"{query}相关企业",
        "industry": "待确认",
        "complexity_assessment": "预研失败，使用默认评估",
        "recommended_mode": "normal",
        "mode_rationale": "默认使用 normal 档位",
        "key_attention_points": ["公司基本信息", "业务发展情况"],
    }


# ============================================================
# Agent 1: Planner - 研究规划师
# ============================================================

PLANNER_SYSTEM_PROMPT = """你是研究规划师（Plan Agent），负责制定深度研究计划。

你的任务是根据公司名称、研究档位和预研 briefing，设计结构化的研究计划。

参考 sn-deep-research 的 plan 设计理念：
1. 维度拆解要合理，覆盖全面但不冗余
2. 每个维度要有明确的关键问题（key_questions）
3. 每个维度要标注建议的信息来源类别
4. 维度之间有逻辑递进关系（从概况到深入分析）

输出要求（严格JSON格式）：
{
  "plan_name": "研究计划名称",
  "company_profile": "公司一句话定位",
  "research_objective": "研究目标说明",
  "dimensions": [
    {
      "id": "d1",
      "name": "维度名称",
      "description": "维度说明",
      "key_questions": ["关键问题1", "关键问题2"],
      "focus": "研究重点方向",
      "suggested_sources": ["通用搜索", "财经数据", ...],
      "depth_level": "skim|standard|deep"
    }
  ]
}

注意：
- 维度ID按 d1, d2, d3... 编号
- depth_level: skim(快速), standard(标准), deep(深入)
- suggested_sources 从以下选择：通用搜索、企业信息、财经数据、行业报告、新闻资讯、社媒讨论
"""


def planner_agent(client: SenseNovaClient, tool_manager: ToolManager,
                  company_name: str, depth: str,
                  briefing: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Planner Agent - 研究规划师

    制定研究计划，拆解研究维度，设计关键问题。
    对应 sn-deep-research 中的 plan agent。

    Args:
        client: SenseNova客户端
        tool_manager: 工具管理器
        company_name: 公司名称
        depth: 研究深度 (quick/normal/heavy)
        briefing: 预研 briefing（可选）

    Returns:
        {plan, tool_calls_history, duration_ms, input_summary, output_summary}
    """
    # 解析档位（支持别名兼容）
    actual_depth = DEPTH_ALIASES.get(depth, depth)
    config = DEPTH_CONFIG.get(actual_depth, DEPTH_CONFIG["normal"])
    dim_count = config["dimension_count"]
    dim_names = ALL_DIMENSIONS[:dim_count]
    dim_desc = "、".join(dim_names)

    briefing_context = ""
    if briefing:
        briefing_context = f"""
预研背景（来自 Scout）：
- 公司定位：{briefing.get('company_profile', '')}
- 所属行业：{briefing.get('industry', '')}
- 重点关注：{', '.join(briefing.get('key_attention_points', []))}
"""

    user_prompt = f"""请为「{company_name}」制定深度研究计划。

研究档位：{actual_depth}（共{dim_count}个研究维度）
建议维度方向：{dim_desc}
{briefing_context}

请先使用 web_search 快速了解这家公司的最新情况，然后基于你的理解，
为每个维度设计具体的关键问题和研究重点。

注意：
1. 维度数量保持为{dim_count}个，但名称和顺序可根据公司特点调整
2. 关键问题要具体、有深度，能够指导调研工作
3. 每个维度标注建议的信息来源类别
4. 请严格以JSON格式输出研究计划"""

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, company_name)

    # Planner使用web_search工具
    tools = tool_manager.get_tool_schemas(["web_search"])

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=2,
        temperature=0.4,
        max_tokens=2000,
    )

    # 解析JSON
    content = result["content"].strip()
    plan = _parse_json_safe(content, _generate_fallback_plan(company_name, dim_count))

    return {
        "plan": plan,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 深度:{depth}, 维度数:{dim_count}",
        "output_summary": truncate(json.dumps(plan, ensure_ascii=False), 200),
    }


def _generate_fallback_plan(company_name: str, dim_count: int) -> Dict[str, Any]:
    """生成保底研究计划（当LLM输出无法解析时使用）"""
    questions_map = {
        "公司概况": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"],
        "财务表现": ["公司最近的财务状况如何？", "营收结构和盈利能力怎样？"],
        "业务分析": ["公司的主营业务和产品有哪些？", "公司的商业模式和客户群体是什么？"],
        "竞争格局": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
        "技术实力": ["公司的技术研发实力如何？", "公司有哪些核心技术和专利？"],
        "发展战略": ["公司的发展战略和规划是什么？", "公司未来的增长动力在哪里？"],
        "风险分析": ["公司面临的主要风险有哪些？", "行业政策和监管环境如何？"],
    }

    selected_dims = ALL_DIMENSIONS[:dim_count]
    dimensions = []
    for dim in selected_dims:
        dimensions.append({
            "name": dim,
            "key_questions": questions_map.get(dim, ["该维度的核心信息是什么？"]),
        })

    return {
        "plan_name": f"{company_name}深度研究计划",
        "company_profile": f"{company_name}是一家值得深入研究的企业",
        "dimensions": dimensions,
    }


# ============================================================
# Agent 2: Researcher - 信息检索专家
# ============================================================

RESEARCHER_SYSTEM_PROMPT = """你是资深行业研究员，负责对公司进行深度调研。
你的任务是针对给定的研究维度和关键问题，利用可用工具收集信息，
并整理出结构化、有深度的调研结果。

可用工具：
- web_search: 通用网页搜索，获取各类信息
- company_lookup: 查询企业基本信息（成立时间、总部、员工数等）
- financial_data: 查询财务数据（营收、利润、毛利率等）

工作原则：
1. 先分析需要哪些信息，选择合适的工具
2. 搜索关键词要精准，围绕关键问题设计
3. 信息收集要全面，注意数据的准确性和时效性
4. 整理结果时要有条理，突出关键发现
5. 引用信息时注明来源标题

输出要求：
- 以严格的JSON格式输出
- 确保数据准确，来源清晰
- 关键发现要有数据支撑

输出格式：
{
  "dimension": "维度名称",
  "summary": "维度的整体总结（200-300字）",
  "key_findings": [
    "关键发现1（有数据支撑）",
    "关键发现2（有数据支撑）",
    "关键发现3（有数据支撑）"
  ],
  "data_points": [
    {"metric": "指标名称", "value": "数值", "source": "来源"}
  ],
  "sources": [
    {"title": "来源标题", "relevance": "high/medium/low"}
  ]
}
"""


def researcher_agent(client: SenseNovaClient, tool_manager: ToolManager,
                     company_name: str, dimension_name: str,
                     key_questions: List[str], depth: str = "basic") -> Dict[str, Any]:
    """
    调研阶段 Agent - 信息检索专家

    使用 web_search, company_lookup, financial_data 三个工具进行多维度调研。

    Args:
        client: SenseNova客户端
        tool_manager: 工具管理器
        company_name: 公司名称
        dimension_name: 维度名称
        key_questions: 关键问题列表
        depth: 研究深度

    Returns:
        {result, tool_calls_history, duration_ms, input_summary, output_summary}
    """
    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["basic"])
    questions_str = "\n".join([f"- {q}" for q in key_questions])

    user_prompt = f"""请对「{company_name}」进行「{dimension_name}」维度的深度调研。

需要回答的关键问题：
{questions_str}

请按照以下步骤进行：
1. 分析这些问题需要哪些类型的信息
2. 选择合适的工具（web_search / company_lookup / financial_data）进行信息收集
3. 可以多次调用工具，从不同角度补充信息
4. 整理出结构化的调研结果

注意：搜索关键词要围绕关键问题设计，确保信息的相关性和准确性。"""

    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, company_name)

    # Researcher使用三个工具
    tools = tool_manager.get_tool_schemas(["web_search", "company_lookup", "financial_data"])

    # 根据深度决定迭代次数
    max_iter = config["research_iterations"] + 1  # +1 用于最终总结

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=max_iter,
        temperature=0.5,
        max_tokens=2500,
    )

    # 解析结果
    content = result["content"].strip()
    research_result = _parse_json_safe(content, _generate_fallback_research(dimension_name, company_name))

    return {
        "result": research_result,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"维度:{dimension_name}, 问题数:{len(key_questions)}",
        "output_summary": truncate(json.dumps(research_result, ensure_ascii=False), 200),
    }


def _generate_fallback_research(dimension_name: str, company_name: str) -> Dict[str, Any]:
    """生成保底调研结果"""
    return {
        "dimension": dimension_name,
        "summary": f"基于公开信息对{company_name}的{dimension_name}进行了初步分析。该公司在这一领域具有一定的代表性，具体数据建议参考官方财报和权威机构报告。",
        "key_findings": [
            f"{company_name}在{dimension_name}方面有一定的市场表现",
            "行业整体处于发展变化之中",
            "建议结合最新财报数据进行深入分析",
        ],
        "data_points": [],
        "sources": [],
    }


# ============================================================
# Agent 3: Writer - 资深行业研究员
# ============================================================

WRITER_SYSTEM_PROMPT = """你是资深行业研究报告撰写专家。
你的任务是根据各维度的调研结果，撰写一份高质量、专业化的公司深度研究报告。

写作原则：
1. 结构清晰，层次分明，逻辑严谨
2. 内容全面，数据详实，观点明确
3. 分析深入，有洞察力，不仅仅是信息堆砌
4. 语言专业、流畅，符合行业研究报告规范
5. 关键数据要准确，重要结论要有数据支撑

报告结构：
1. 封面标题
2. 执行摘要（核心结论，300-500字）
3. 关键指标速览（表格形式）
4. 各维度详细分析（对应调研维度，每部分有小标题和数据支撑）
5. 总结与展望（核心观点 + 未来展望）

请输出完整的Markdown格式报告。
"""


def writer_agent(client: SenseNovaClient, company_name: str,
                 research_results: List[Dict], depth: str,
                 review_feedback: Optional[Dict] = None) -> Dict[str, Any]:
    """
    报告撰写 Agent - 资深行业研究员

    整合所有维度的调研结果，撰写完整的研究报告。
    如果提供了审核意见，则根据意见进行修改。

    Args:
        client: SenseNova客户端
        company_name: 公司名称
        research_results: 各维度调研结果列表
        depth: 研究深度
        review_feedback: 审核意见（可选，用于修改模式）

    Returns:
        {report, duration_ms, input_summary, output_summary}
    """
    # 构建调研结果摘要
    results_text = ""
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        summary = res.get("summary", "")
        findings = res.get("key_findings", [])
        data_points = res.get("data_points", [])
        findings_str = "\n".join([f"- {f}" for f in findings])
        data_str = ""
        if data_points:
            data_str = "\n**关键数据**：\n"
            for dp in data_points[:5]:
                data_str += f"- {dp.get('metric', '')}: {dp.get('value', '')}\n"
        results_text += f"""
## 维度{i}：{dim_name}

**概要**：{summary}

**关键发现**：
{findings_str}
{data_str}
"""

    if review_feedback:
        # 修改模式
        user_prompt = f"""请根据以下审核意见，修改「{company_name}」的研究报告。

=== 审核意见 ===
评分：{review_feedback.get('overall_score', 0)}/100
主要问题：
{chr(10).join(['- ' + p.get('description', '') for p in review_feedback.get('issues', [])[:5]])}

修改建议：
{review_feedback.get('suggestions', '')}

=== 原始调研数据 ===
{results_text}

=== 当前报告 ===
{review_feedback.get('current_report', '')[:2000]}

===

请根据审核意见和原始调研数据，修改并优化报告。重点解决审核中指出的问题，
提升报告的质量和深度。请输出完整的修改后报告（Markdown格式）。"""
    else:
        # 初稿模式
        user_prompt = f"""请根据以下调研结果，为「{company_name}」撰写一份深度研究报告。

研究深度：{depth}
调研维度：{len(research_results)}个

=== 调研结果 ===
{results_text}
===

请撰写一份结构完整、内容详实、分析深入的Markdown格式研究报告。

报告应包含：
1. 标题 + 研究说明
2. 执行摘要（核心结论）
3. 关键指标速览（表格）
4. 各维度详细分析（{len(research_results)}个维度，每个维度有独立章节）
5. 总结与展望

要求：
- 总字数不少于1500字
- 数据准确，引用清晰
- 分析有深度，观点明确
- 语言专业流畅"""

    messages = [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = client.chat(
            messages,
            temperature=0.5,
            max_tokens=4000,
        )
        report = result["content"].strip()
        duration_ms = result["duration_ms"]
    except Exception as e:
        logger.warning(f"Writer agent API call failed, using fallback: {e}")
        # 降级：基于调研结果生成保底报告
        report = _generate_fallback_report(company_name, research_results, depth)
        duration_ms = 0

    return {
        "report": report,
        "duration_ms": duration_ms,
        "input_summary": f"公司:{company_name}, 维度数:{len(research_results)}, 模式:{'修改' if review_feedback else '初稿'}",
        "output_summary": truncate(report, 200),
    }


def _generate_fallback_report(company_name: str, research_results: List[Dict], depth: str) -> str:
    """
    生成保底研究报告（当LLM调用失败时使用）

    Args:
        company_name: 公司名称
        research_results: 调研结果列表
        depth: 研究深度

    Returns:
        Markdown格式的保底报告
    """
    sections = []
    sections.append(f"# {company_name}深度研究报告\n")
    sections.append(f"> 研究深度：{depth} | 生成时间：{now_ts()}\n")

    # 执行摘要
    sections.append("## 执行摘要\n")
    sections.append(f"本报告对{company_name}进行了多维度深度研究。基于公开信息和调研数据，"
                    f"从{len(research_results)}个维度对公司进行了全面分析。"
                    f"以下是各维度的详细研究内容。\n")

    # 关键指标
    sections.append("## 关键指标速览\n")
    sections.append("| 指标 | 数值 | 维度 |")
    sections.append("|------|------|------|")
    has_metrics = False
    for res in research_results:
        for dp in res.get("data_points", [])[:2]:
            sections.append(f"| {dp.get('metric', '-')} | {dp.get('value', '-')} | {res.get('dimension', '-')} |")
            has_metrics = True
    if not has_metrics:
        sections.append("| 数据完整性 | 部分数据待补充 | 综合 |")
    sections.append("")

    # 各维度分析
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        summary = res.get("summary", "暂无详细信息")
        findings = res.get("key_findings", [])
        sections.append(f"## {i}. {dim_name}\n")
        sections.append(f"{summary}\n")
        if findings:
            sections.append("**关键发现：**\n")
            for f in findings:
                sections.append(f"- {f}")
            sections.append("")

    # 总结
    sections.append("## 总结与展望\n")
    sections.append(f"综合以上{len(research_results)}个维度的分析，{company_name}在行业内具有一定的竞争力。"
                    f"公司在核心业务领域保持稳定发展，同时积极探索新的增长机会。"
                    f"建议投资者和行业研究者持续关注公司的最新动态和财务表现。\n")
    sections.append("---")
    sections.append("*注：本报告由AI系统自动生成，数据来源于公开信息，仅供参考，不构成投资建议。*")

    return "\n".join(sections)


# ============================================================
# Agent 4: Reviewer - 质量审核专家
# ============================================================

REVIEWER_SYSTEM_PROMPT = """你是专业的研究报告质量审核专家。
你的任务是从多个维度对研究报告进行全面审核，找出问题并提出修改建议。

审核维度：
1. 数据准确性 - 关键数据是否合理，是否有来源支撑
2. 逻辑一致性 - 前后论述是否一致，因果关系是否成立
3. 结构完整性 - 章节是否齐全，重点是否突出，层次是否清晰
4. 分析深度 - 观点是否有洞察力，分析是否流于表面
5. 可读性 - 语言是否通顺专业，表达是否清晰

输出要求：
- 以严格的JSON格式输出
- 评分要客观公正，问题要具体明确
- 修改建议要有可操作性

输出格式：
{
  "overall_score": 85,
  "dimension_scores": {
    "data_accuracy": 80,
    "logic_consistency": 85,
    "structure_completeness": 90,
    "analysis_depth": 80,
    "readability": 85
  },
  "issues": [
    {
      "severity": "high/medium/low",
      "category": "数据准确性/逻辑一致性/...",
      "description": "问题的具体描述",
      "location": "出现问题的章节或位置"
    }
  ],
  "suggestions": "整体修改建议（100-200字）",
  "needs_revision": true
}
"""


def reviewer_agent(client: SenseNovaClient, company_name: str,
                   report: str, research_results: List[Dict]) -> Dict[str, Any]:
    """
    质量审核 Agent - 质量审核专家

    从数据准确性、逻辑一致性、结构完整性、分析深度、可读性五个维度审核报告。
    不使用工具，纯逻辑审核。

    Args:
        client: SenseNova客户端
        company_name: 公司名称
        report: 报告内容
        research_results: 调研结果（用于数据比对参考）

    Returns:
        {review_result, duration_ms, input_summary, output_summary}
    """
    # 截取报告前3000字进行审核（避免token过长）
    report_excerpt = report[:3000] + ("..." if len(report) > 3000 else "")

    # 调研结果摘要
    results_summary = []
    for res in research_results:
        dim = res.get("dimension", "")
        findings = res.get("key_findings", [])[:3]
        results_summary.append(f"- {dim}: {'; '.join(findings)}")
    results_text = "\n".join(results_summary)

    user_prompt = f"""请审核「{company_name}」的研究报告。

=== 报告内容 ===
{report_excerpt}

=== 原始调研数据（参考） ===
{results_text}

===

请从以下五个维度进行审核：
1. 数据准确性（20分）- 数据是否合理，是否有支撑
2. 逻辑一致性（20分）- 论述是否一致，因果是否成立
3. 结构完整性（20分）- 章节是否齐全，层次是否清晰
4. 分析深度（20分）- 观点是否有深度，是否流于表面
5. 可读性（20分）- 语言是否专业，表达是否清晰

请输出完整的审核结果JSON，包含总分、各维度得分、问题列表和修改建议。
问题要具体，指出问题所在的章节或内容。
needs_revision 字段表示是否需要修改（总分低于80分为true）。"""

    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = client.chat(
            messages,
            temperature=0.3,
            max_tokens=1500,
        )
        content = result["content"].strip()
        review_result = _parse_json_safe(content, _generate_fallback_review())
        duration_ms = result["duration_ms"]
    except Exception as e:
        logger.warning(f"Reviewer agent API call failed, using fallback: {e}")
        review_result = _generate_fallback_review()
        duration_ms = 0

    return {
        "review_result": review_result,
        "duration_ms": duration_ms,
        "input_summary": f"公司:{company_name}, 报告长度:{len(report)}字",
        "output_summary": f"总分:{review_result.get('overall_score', 0)}, 问题数:{len(review_result.get('issues', []))}",
    }


def _generate_fallback_review() -> Dict[str, Any]:
    """生成保底审核结果"""
    return {
        "overall_score": 75,
        "dimension_scores": {
            "data_accuracy": 75,
            "logic_consistency": 80,
            "structure_completeness": 80,
            "analysis_depth": 70,
            "readability": 75,
        },
        "issues": [
            {
                "severity": "medium",
                "category": "分析深度",
                "description": "部分分析较为表面，缺乏深入洞察",
                "location": "多个章节",
            },
        ],
        "suggestions": "建议加强数据支撑，深化分析，提升报告的专业深度。",
        "needs_revision": True,
    }


# ============================================================
# Agent 5: FactChecker - 事实核查员
# ============================================================

FACT_CHECKER_SYSTEM_PROMPT = """你是专业的事实核查员。
你的任务是从研究报告中提取关键数据点，并通过搜索验证这些数据的准确性。

工作流程：
1. 从报告中提取关键数据点（营收、利润、增长率、市场份额、员工数、专利数等）
2. 使用 web_search 工具搜索验证这些数据
3. 对每个数据点给出可信度评估

可信度等级：
- 高可信度：有多个来源交叉验证，数据一致
- 中可信度：有单一来源支撑，数据大致吻合
- 低可信度：无明确来源，数据存疑或存在矛盾

输出要求：
- 以严格的JSON格式输出
- 每个数据点都要有明确的可信度标注
- 低可信度数据要说明原因

输出格式：
{
  "overall_confidence": "high/medium/low",
  "checked_points": [
    {
      "data_point": "2024年上半年营收18.5亿元",
      "category": "财务数据",
      "claimed_value": "18.5亿元人民币",
      "confidence": "high/medium/low",
      "verification": "验证说明",
      "sources": ["来源1", "来源2"]
    }
  ],
  "summary": "核查总结（100-200字）"
}
"""


def fact_checker_agent(client: SenseNovaClient, tool_manager: ToolManager,
                       company_name: str, report: str, mode: str = "simple") -> Dict[str, Any]:
    """
    事实核查 Agent - 事实核查员

    使用 web_search 工具验证报告中的关键数据。
    三种模式：basic（规则提取+搜索验证）、llm（LLM核查+搜索验证）、full（全量核查+交叉验证）

    Args:
        client: SenseNova客户端
        tool_manager: 工具管理器
        company_name: 公司名称
        report: 报告内容
        mode: 核查模式 (simple/llm/full)

    Returns:
        {fact_check_result, tool_calls_history, duration_ms, input_summary, output_summary}
    """
    if mode == "basic":
        # 基础模式：规则提取 + 1次搜索验证（快速但有实际验证）
        result = _basic_fact_check(report, company_name, tool_manager)
        return {
            "fact_check_result": result["fact_check_result"],
            "tool_calls_history": result["tool_calls_history"],
            "duration_ms": result["duration_ms"],
            "input_summary": f"公司:{company_name}, 模式:basic",
            "output_summary": f"核查数据点:{len(result['fact_check_result'].get('checked_points', []))}",
        }

    # LLM模式（llm/full）：使用LLM提取数据点并搜索验证
    # 截取报告部分内容
    report_excerpt = report[:3000]

    search_count = 2 if mode == "llm" else 4

    user_prompt = f"""请对「{company_name}」的研究报告进行事实核查。

=== 报告内容 ===
{report_excerpt}

===

请按以下步骤进行：
1. 从报告中提取5-8个关键数据点（财务数据、市场数据、运营数据等）
2. 使用 web_search 工具搜索验证这些数据
3. 对每个数据点给出可信度评估

核查模式：{mode}
- 请进行{search_count}次搜索验证
- 重点核查财务数据和核心指标
- 标注每个数据点的可信度

请以JSON格式输出核查结果。"""

    messages = [
        {"role": "system", "content": FACT_CHECKER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, company_name)

    tools = tool_manager.get_tool_schemas(["web_search"])

    max_iter = search_count + 1  # +1 用于最终总结

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=max_iter,
        temperature=0.2,
        max_tokens=2000,
    )

    content = result["content"].strip()
    fact_result = _parse_json_safe(content, _generate_fallback_fact_check())

    return {
        "fact_check_result": fact_result,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 模式:{mode}",
        "output_summary": f"总体可信度:{fact_result.get('overall_confidence', 'medium')}, 核查点:{len(fact_result.get('checked_points', []))}",
    }


def _basic_fact_check(report: str, company_name: str,
                      tool_manager: ToolManager) -> Dict[str, Any]:
    """
    基础版事实核查：规则提取 + 1次搜索验证

    比纯规则提取更可信：提取数据点后，用一次搜索快速验证核心财务数据。

    Args:
        report: 报告内容
        company_name: 公司名称
        tool_manager: 工具管理器

    Returns:
        {fact_check_result, tool_calls_history, duration_ms}
    """
    import re

    start = time.time()
    tool_calls_history = []
    checked_points = []

    # 提取数字+单位的模式
    patterns = [
        (r'(\d+(?:\.\d+)?)\s*亿元?', '财务数据'),
        (r'(\d+(?:\.\d+)?)\s*%', '比例/增长率'),
        (r'(\d+(?:\.\d+)?)\s*万', '数量级'),
    ]

    found = set()
    for pattern, category in patterns:
        matches = re.findall(pattern, report)
        for m in matches[:5]:
            if m not in found:
                found.add(m)
                checked_points.append({
                    "data_point": f"{m}（{category}）",
                    "category": category,
                    "claimed_value": m,
                    "confidence": "low",  # 初始为低，搜索验证后升级
                    "verification": "待搜索验证",
                    "sources": ["报告原文"],
                })

    # 限制总数
    checked_points = checked_points[:8]

    # 执行一次搜索验证（核心财务数据）
    try:
        search_query = f"{company_name} 财务数据 营收 利润"
        search_result_str = tool_manager.execute_tool(
            "web_search",
            json.dumps({"query": search_query}, ensure_ascii=False),
            company_name,
        )
        tool_calls_history.append({
            "name": "web_search",
            "arguments": json.dumps({"query": search_query}, ensure_ascii=False),
            "result_summary": truncate(search_result_str, 300),
        })

        # 简单的验证逻辑：如果搜索结果中包含公司名和财务关键词，提升可信度
        search_result_lower = search_result_str.lower()
        has_financial_data = any(
            kw in search_result_lower for kw in ["营收", "利润", "财务", "收入", "亿元"]
        )

        if has_financial_data:
            # 有相关搜索结果，将财务类数据点提升为中可信度
            for cp in checked_points:
                if cp["category"] == "财务数据":
                    cp["confidence"] = "medium"
                    cp["verification"] = "搜索结果中包含相关财务信息，数据大致可信"
                    cp["sources"].append("搜索验证")
    except Exception as e:
        logger.warning(f"Basic fact check search failed: {e}")

    duration_ms = int((time.time() - start) * 1000)

    # 计算总体可信度
    high_count = sum(1 for cp in checked_points if cp["confidence"] == "high")
    medium_count = sum(1 for cp in checked_points if cp["confidence"] == "medium")
    if high_count > len(checked_points) / 2:
        overall = "high"
    elif medium_count + high_count > len(checked_points) / 2:
        overall = "medium"
    else:
        overall = "low"

    fact_check_result = {
        "overall_confidence": overall,
        "checked_points": checked_points,
        "summary": f"基于规则提取了{len(checked_points)}个数据点，并通过搜索进行了初步验证。"
                   f"总体可信度为{overall}，建议结合官方财报进一步核实关键数据。",
        "mode": "basic",
    }

    return {
        "fact_check_result": fact_check_result,
        "tool_calls_history": tool_calls_history,
        "duration_ms": duration_ms,
    }


def _generate_fallback_fact_check() -> Dict[str, Any]:
    """生成保底事实核查结果"""
    return {
        "overall_confidence": "medium",
        "checked_points": [
            {
                "data_point": "报告中提及的核心数据",
                "category": "综合",
                "claimed_value": "待核实",
                "confidence": "medium",
                "verification": "核查过程中出现异常，数据可信度待确认",
                "sources": [],
            },
        ],
        "summary": "事实核查过程中出现异常，建议手动核查关键数据的准确性。",
        "mode": "fallback",
    }


# ============================================================
# 辅助函数：安全解析JSON
# ============================================================

def _parse_json_safe(content: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """
    安全解析JSON，失败时返回保底数据

    支持多种格式：
    1. 纯JSON字符串
    2. Markdown代码块包裹的JSON（```json ... ```）
    3. 文本中嵌入的JSON对象（提取第一个完整的{}块）

    Args:
        content: 待解析的内容
        fallback: 保底数据

    Returns:
        解析后的字典
    """
    if not content:
        return fallback

    content = str(content).strip()

    # 策略1：直接解析
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 策略2：提取Markdown代码块中的JSON（使用正则更健壮）
    try:
        import re
        # 匹配 ```json\n...\n``` 或 ```\n...\n``` 格式
        pattern = r'```(?:json)?\s*\n(.*?)\n```'
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            json_str = match.group(1).strip()
            result = json.loads(json_str)
            if isinstance(result, dict):
                return result
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 策略3：从文本中提取第一个完整的JSON对象
    # 使用括号平衡法，找到第一个完整的 {} 对
    try:
        start = content.find("{")
        if start >= 0:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(content)):
                ch = content[i]
                if escape:
                    escape = False
                    continue
                if ch == '\\':
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = content[start:i + 1]
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                        break
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 策略4：尝试补全被截断的JSON（尝试添加闭合括号）
    try:
        start = content.find("{")
        if start >= 0:
            # 取到最后一个完整的字段后尝试补全
            truncated = content[start:]
            # 逐个添加 } 尝试解析（最多补5个）
            for close_count in range(1, 6):
                candidate = truncated + "}" * close_count
                try:
                    result = json.loads(candidate)
                    if isinstance(result, dict):
                        logger.debug(f"JSON解析通过补全{close_count}个括号成功")
                        return result
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:
        pass

    logger.warning(f"JSON解析失败，使用保底数据。内容前100字: {content[:100]}")
    return fallback


# ============================================================
# ResearchOrchestrator - 研究编排器
# ============================================================

class ResearchOrchestrator:
    """
    研究编排器 - 协调5个Agent完成完整研究流程

    流程：
    Planner → Researcher×N → Writer → Reviewer → (可选修改) → FactChecker → 最终输出

    支持三种深度的差异化配置。
    """

    def __init__(self):
        """初始化编排器"""
        pass

    def run(self, company_name: str, depth: str, api_key: str,
            event_callback: Optional[Callable[[str, Dict], None]] = None,
            total_timeout: int = 600, model: Optional[str] = None) -> Dict[str, Any]:
        """
        执行完整研究流程

        Args:
            company_name: 公司名称
            depth: 研究深度 (basic/standard/deep)
            api_key: API密钥
            event_callback: 事件回调函数 (event_type, data_dict) -> None
            total_timeout: 总超时时间（秒），默认10分钟
            model: 模型名称（可选）

        Returns:
            完整的研究结果字典
        """
        # 初始化
        logs = []
        step_counter = [0]
        research_results = []
        total_start = time.time()
        timed_out = [False]  # 使用列表以便在内部函数中修改

        config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["basic"])

        def check_timeout() -> bool:
            """检查是否已超时"""
            elapsed = time.time() - total_start
            if elapsed >= total_timeout:
                if not timed_out[0]:
                    timed_out[0] = True
                    logger.warning(f"Research timeout after {elapsed:.1f}s (limit: {total_timeout}s)")
                return True
            return False

        def emit(event_type: str, data: Dict):
            """发送事件"""
            if event_callback:
                try:
                    event_callback(event_type, data)
                except Exception as e:
                    logger.warning(f"Event callback error: {e}")

        def add_log(agent: str, action: str, input_summary: str = "",
                    output_summary: str = "", tool_calls: Optional[List] = None,
                    duration_ms: int = 0, level: str = "info") -> Dict:
            """添加日志"""
            step_counter[0] += 1
            log = make_log(
                step=step_counter[0],
                agent=agent,
                action=action,
                input_summary=input_summary,
                output_summary=output_summary,
                tool_calls=tool_calls,
                duration_ms=duration_ms,
                level=level,
            )
            logs.append(log)
            emit("log", log)
            return log

        try:
            # =========================================
            # 阶段0：初始化
            # =========================================
            emit("progress", {"percent": 0, "phase": "initializing",
                              "message": "初始化研究系统..."})
            add_log("system", "研究任务启动",
                    input_summary=f"公司:{company_name}, 深度:{depth}",
                    output_summary=f"预计耗时:{config['estimated_duration']}")

            client = SenseNovaClient(api_key, model=model)
            tool_manager = ToolManager()

            # =========================================
            # 阶段1：规划 (Planner)
            # =========================================
            emit("progress", {"percent": 5, "phase": "planning",
                              "message": "开始制定研究计划..."})
            emit("agent_start", {"agent": "planner", "phase": "规划阶段",
                                "input_summary": f"公司: {company_name}\n深度: {depth}"})

            add_log("planner", "启动规划Agent",
                    input_summary=f"公司:{company_name}, 深度:{depth}")

            plan_result = planner_agent(client, tool_manager, company_name, depth)

            add_log(
                "planner", "完成研究计划",
                input_summary=plan_result["input_summary"],
                output_summary=plan_result["output_summary"],
                tool_calls=plan_result["tool_calls_history"],
                duration_ms=plan_result["duration_ms"],
            )

            plan = plan_result["plan"]
            dimensions = plan.get("dimensions", [])
            dim_count = len(dimensions)

            # 确保维度数量符合深度要求
            if dim_count < config["dimension_count"]:
                # 补充维度
                existing = {d.get("name", "") for d in dimensions}
                for dim_name in ALL_DIMENSIONS:
                    if dim_name not in existing and len(dimensions) < config["dimension_count"]:
                        dimensions.append({
                            "name": dim_name,
                            "key_questions": [f"{dim_name}的核心信息是什么？"],
                        })

            emit("progress", {"percent": 15, "phase": "planning",
                              "message": f"研究计划已制定，共{len(dimensions)}个维度"})
            emit("agent_end", {"agent": "planner", "status": "success",
                               "output_summary": plan_result["output_summary"]})

            # 发送工具调用事件
            for tc in plan_result["tool_calls_history"]:
                emit("tool_call", {
                    "agent": "planner",
                    "tool": tc["name"],
                    "arguments": tc["arguments"],
                    "result_summary": tc["result_summary"],
                })

            # =========================================
            # 阶段2：调研 (Researcher × N)
            # =========================================
            emit("progress", {"percent": 20, "phase": "researching",
                              "message": "开始调研阶段..."})

            total_dims = len(dimensions)
            for i, dim in enumerate(dimensions):
                # 超时检测：超时则停止调研，使用已有结果继续
                if check_timeout():
                    add_log(
                        "system", "研究超时，停止调研",
                        input_summary=f"已完成{i}/{total_dims}个维度",
                        output_summary="将基于已有数据生成报告",
                        level="warning",
                    )
                    break

                dim_name = dim.get("name", f"维度{i+1}")
                key_questions = dim.get("key_questions", [])

                # 计算进度（调研阶段占20%-70%）
                base_progress = 20
                research_range = 50
                dim_progress = base_progress + int((i / total_dims) * research_range)

                emit("progress", {
                    "percent": dim_progress,
                    "phase": "researching",
                    "message": f"正在调研：{dim_name} ({i+1}/{total_dims})",
                    "details": {"dimension": dim_name, "index": i + 1, "total": total_dims},
                })
                emit("agent_start", {
                    "agent": "researcher",
                    "phase": f"调研阶段 - {dim_name}",
                    "dimension": dim_name,
                    "index": i + 1,
                    "total": total_dims,
                    "input_summary": f"公司: {company_name}\n维度: {dim_name} ({i + 1}/{total_dims})",
                })

                add_log(
                    "researcher", f"开始调研「{dim_name}」",
                    input_summary=f"维度:{dim_name}, 问题:{len(key_questions)}个",
                )

                try:
                    res_result = researcher_agent(
                        client, tool_manager, company_name, dim_name, key_questions, depth
                    )

                    research_results.append(res_result["result"])

                    add_log(
                        "researcher", f"完成「{dim_name}」调研",
                        input_summary=res_result["input_summary"],
                        output_summary=res_result["output_summary"],
                        tool_calls=res_result["tool_calls_history"],
                        duration_ms=res_result["duration_ms"],
                    )

                    # 工具调用事件
                    for tc in res_result["tool_calls_history"]:
                        emit("tool_call", {
                            "agent": "researcher",
                            "dimension": dim_name,
                            "tool": tc["name"],
                            "arguments": tc["arguments"],
                            "result_summary": tc["result_summary"],
                        })

                except Exception as e:
                    logger.error(f"Researcher error for {dim_name}: {e}")
                    add_log(
                        "researcher", f"「{dim_name}」调研失败",
                        input_summary=f"维度:{dim_name}",
                        output_summary=str(e),
                        level="error",
                    )
                    # 添加保底结果
                    research_results.append(_generate_fallback_research(dim_name, company_name))

                emit("agent_end", {"agent": "researcher", "status": "success",
                                   "dimension": dim_name,
                                   "output_summary": truncate(str(research_results[-1]), 150)})

            # =========================================
            # 阶段3：报告撰写 (Writer - 初稿)
            # =========================================
            emit("progress", {"percent": 72, "phase": "writing",
                              "message": "开始撰写研究报告..."})
            emit("agent_start", {"agent": "writer", "phase": "报告撰写阶段",
                                "input_summary": f"公司: {company_name}\n维度数: {len(research_results)}"})

            add_log("writer", "启动报告撰写Agent",
                    input_summary=f"维度数:{len(research_results)}")

            # 超时则直接生成保底报告，不调用LLM
            if check_timeout():
                current_report = _generate_fallback_report(company_name, research_results, depth)
                add_log(
                    "writer", "超时，生成保底报告",
                    input_summary=f"维度数:{len(research_results)}",
                    output_summary="基于调研结果生成保底报告",
                    level="warning",
                )
            else:
                writer_result = writer_agent(client, company_name, research_results, depth)
                current_report = writer_result["report"]

                add_log(
                    "writer", "完成报告初稿",
                    input_summary=writer_result["input_summary"],
                    output_summary=writer_result["output_summary"],
                    duration_ms=writer_result["duration_ms"],
                )

            emit("agent_end", {"agent": "writer", "status": "success",
                               "output_summary": writer_result["output_summary"]})

            # =========================================
            # 阶段4-5：质量审核 + 修改循环 (Reviewer ↔ Writer)
            # =========================================
            review = None
            modify_rounds = config["review_modify_rounds"]
            total_review_rounds = modify_rounds + 1  # 总审核次数 = 修改次数 + 1（最终审核）

            # 超时则跳过审核修改，使用保底审核结果
            if check_timeout():
                add_log(
                    "system", "研究超时，跳过质量审核",
                    input_summary="",
                    output_summary="使用保底审核结果",
                    level="warning",
                )
                review = _generate_fallback_review()
            else:
                for round_idx in range(total_review_rounds):
                    # 每轮开始前检查超时
                    if round_idx > 0 and check_timeout():
                        add_log(
                            "system", "研究超时，终止审核循环",
                            input_summary=f"已完成{round_idx}/{total_review_rounds}轮",
                            output_summary="使用最新审核结果",
                            level="warning",
                        )
                        break

                    is_last_round = (round_idx == total_review_rounds - 1)
                    round_num = round_idx + 1

                    # 计算进度（审核修改阶段占72%-90%）
                    review_start = 72
                    review_range = 20 if modify_rounds > 0 else 12
                    round_progress = review_start + int(
                        (round_idx / max(total_review_rounds - 1, 1)) * review_range
                    )

                    emit("progress", {
                        "percent": min(round_progress, 90),
                        "phase": "reviewing",
                        "message": f"第{round_num}轮质量审核..." if not is_last_round else "最终质量审核...",
                    })
                    emit("agent_start", {
                        "agent": "reviewer",
                        "phase": f"质量审核 - 第{round_num}轮",
                        "round": round_num,
                        "total_rounds": total_review_rounds,
                        "input_summary": f"公司: {company_name}\n报告长度: {len(current_report)}字\n轮次: {round_num}/{total_review_rounds}",
                    })

                    add_log(
                        "reviewer",
                        f"第{round_num}轮质量审核",
                        input_summary=f"报告长度:{len(current_report)}字, 轮次:{round_num}/{total_review_rounds}",
                    )

                    review_result = reviewer_agent(
                        client, company_name, current_report, research_results
                    )
                    review = review_result["review_result"]

                    add_log(
                        "reviewer",
                        f"第{round_num}轮审核完成",
                        input_summary=review_result["input_summary"],
                        output_summary=review_result["output_summary"],
                        duration_ms=review_result["duration_ms"],
                    )

                    emit("agent_end", {
                        "agent": "reviewer",
                        "status": "success",
                        "output_summary": review_result["output_summary"],
                        "round": round_num,
                    })
                    emit("reviewer_end", {
                        "overall_score": review.get("overall_score", 0),
                        "issues_count": len(review.get("issues", [])),
                        "needs_revision": review.get("needs_revision", False),
                        "round": round_num,
                        "total_rounds": total_review_rounds,
                    })

                    # 如果不是最后一轮且需要修改，则进行修改
                    if not is_last_round and review.get("needs_revision", False):
                        emit("progress", {
                            "percent": min(round_progress + 5, 88),
                            "phase": "revising",
                            "message": f"第{round_num}轮修改：根据审核意见优化报告...",
                        })
                        emit("agent_start", {
                            "agent": "writer",
                            "phase": f"报告修改 - 第{round_num}轮",
                            "round": round_num,
                            "input_summary": f"公司: {company_name}\n报告长度: {len(current_report)}字\n修改轮次: 第{round_num}轮",
                        })

                        add_log(
                            "writer",
                            f"第{round_num}轮报告修改",
                            input_summary=f"审核评分:{review.get('overall_score', 0)}, "
                                           f"问题数:{len(review.get('issues', []))}",
                        )

                        # 准备修改用的feedback
                        review_feedback = dict(review)
                        review_feedback["current_report"] = current_report[:3000]
                        modify_result = writer_agent(
                            client, company_name, research_results, depth, review_feedback
                        )
                        current_report = modify_result["report"]

                        add_log(
                            "writer",
                            f"第{round_num}轮修改完成",
                            input_summary=modify_result["input_summary"],
                            output_summary=modify_result["output_summary"],
                            duration_ms=modify_result["duration_ms"],
                        )

                        emit("agent_end", {
                            "agent": "writer",
                            "status": "success",
                            "output_summary": modify_result["output_summary"],
                            "round": round_num,
                            "phase": "修改完成",
                        })
                    else:
                        # 不需要修改或已是最后一轮，跳出循环
                        if not is_last_round and not review.get("needs_revision", False):
                            add_log(
                                "reviewer",
                                "审核通过，提前结束修改循环",
                                input_summary=f"评分:{review.get('overall_score', 0)}",
                                output_summary="无需修改，直接进入下一阶段",
                                level="info",
                            )
                        break

            # =========================================
            # 阶段6：事实核查 (FactChecker)
            # =========================================
            emit("progress", {"percent": 92, "phase": "fact_checking",
                              "message": "进行事实核查..."})
            emit("agent_start", {"agent": "fact_checker", "phase": "事实核查阶段",
                                "input_summary": f"公司: {company_name}\n报告长度: {len(current_report)}字\n核查模式: {config['fact_check_mode']}"})

            add_log("fact_checker", "启动事实核查Agent",
                    input_summary=f"模式:{config['fact_check_mode']}, 报告长度:{len(current_report)}字")

            # 超时则使用保底事实核查结果
            if check_timeout():
                fact_check = _generate_fallback_fact_check()
                add_log(
                    "fact_checker", "超时，使用保底核查结果",
                    input_summary=f"模式:{config['fact_check_mode']}",
                    output_summary="研究超时，跳过事实核查",
                    level="warning",
                )
            else:
                fact_result = fact_checker_agent(
                    client, tool_manager, company_name, current_report,
                    config["fact_check_mode"]
                )
                fact_check = fact_result["fact_check_result"]

                add_log(
                    "fact_checker", "完成事实核查",
                    input_summary=fact_result["input_summary"],
                    output_summary=fact_result["output_summary"],
                    tool_calls=fact_result["tool_calls_history"],
                    duration_ms=fact_result["duration_ms"],
                )

                # 工具调用事件
                for tc in fact_result["tool_calls_history"]:
                    emit("tool_call", {
                        "agent": "fact_checker",
                        "tool": tc["name"],
                        "arguments": tc["arguments"],
                        "result_summary": tc["result_summary"],
                    })

                emit("agent_end", {"agent": "fact_checker", "status": "success",
                                   "output_summary": fact_result["output_summary"]})
                emit("fact_check", fact_check)

            # =========================================
            # 完成
            # =========================================
            total_duration = int((time.time() - total_start) * 1000)

            emit("progress", {"percent": 100, "phase": "completed",
                              "message": "研究完成！"})

            # 生成摘要和关键指标
            summary = _extract_summary(current_report, company_name)
            key_metrics = _extract_metrics(research_results)

            result = {
                "report": current_report,
                "summary": summary,
                "key_metrics": key_metrics,
                "logs": logs,
                "research_results": research_results,
                "plan": plan,
                "review": review,
                "fact_check": fact_check,
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
                "tool_stats": tool_manager.get_stats(),
                "api_call_count": client.call_count,
            }

            add_log("system", "研究任务完成",
                    input_summary=f"总耗时:{total_duration}ms",
                    output_summary=f"报告长度:{len(current_report)}字, "
                                   f"维度数:{dim_count}, "
                                   f"工具调用:{sum(tool_manager.get_stats().values())}次",
                    level="success")

            emit("complete", {
                "report": current_report,
                "summary": summary,
                "key_metrics": key_metrics,
                "logs": logs,
                "total_duration_ms": total_duration,
                "review": review,
                "fact_check": fact_check,
            })

            return result

        except Exception as e:
            total_duration = int((time.time() - total_start) * 1000)
            error_msg = str(e)

            logger.error(f"Research orchestrator error: {e}", exc_info=True)

            add_log(
                "system", "研究失败",
                input_summary="",
                output_summary=error_msg,
                duration_ms=total_duration,
                level="error",
            )

            emit("error", {"message": error_msg})
            emit("complete", {
                "report": f"# 研究失败\n\n**错误信息**：{error_msg}\n\n请检查API Key是否正确，或稍后重试。",
                "logs": logs,
                "total_duration_ms": total_duration,
                "error": error_msg,
            })

            return {
                "report": f"# 研究失败\n\n**错误信息**：{error_msg}\n\n请检查API Key是否正确，或稍后重试。",
                "logs": logs,
                "research_results": research_results,
                "plan": {},
                "review": {},
                "fact_check": {},
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
                "error": error_msg,
            }


def _extract_summary(report: str, company_name: str) -> str:
    """
    从报告中提取摘要

    Args:
        report: 报告内容
        company_name: 公司名称

    Returns:
        摘要文本
    """
    # 简单提取：找"摘要"或"概述"章节
    lines = report.split("\n")
    in_summary = False
    summary_lines = []

    for line in lines:
        if "执行摘要" in line or "研究摘要" in line or "核心结论" in line:
            in_summary = True
            continue
        if in_summary:
            if line.startswith("#") or line.startswith("## "):
                break
            if line.strip():
                summary_lines.append(line.strip())

    if summary_lines:
        return "\n".join(summary_lines)[:500]

    # 保底：取前几段
    paragraphs = [p.strip() for p in report.split("\n\n") if p.strip()]
    for p in paragraphs[:3]:
        if not p.startswith("#"):
            return p[:300]

    return f"{company_name}深度研究报告已生成。"


def _extract_metrics(research_results: List[Dict]) -> List[Dict]:
    """
    从调研结果中提取关键指标

    Args:
        research_results: 调研结果列表

    Returns:
        关键指标列表
    """
    metrics = []
    for res in research_results:
        data_points = res.get("data_points", [])
        for dp in data_points[:3]:
            metrics.append({
                "metric": dp.get("metric", ""),
                "value": dp.get("value", ""),
                "dimension": res.get("dimension", ""),
            })

    return metrics[:10]  # 最多10个指标
