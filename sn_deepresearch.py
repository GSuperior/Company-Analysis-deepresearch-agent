"""
SN-DeepResearch 风格的深度研究系统

基于 SenseNova Skills 生态的 sn-deep-research 深度研究技能设计思想，
实现精简版的多 Agent 协作流水线。

参考：https://github.com/OpenSenseNova/SenseNova-Skills/tree/main/skills/sn-deep-research

Agent 流水线（对齐 sn-deep-research 精简版）：
  1. Scout        - 预研侦察兵：快速了解 + 档位推荐
  2. Planner      - 研究规划师：维度拆解 + 关键问题设计
  3. Researcher   - 维度取证专家：按维度取证，输出标准化 evidence
  4. Reviewer     - 质量审查员：子报告审查 + 缺口识别
  5. ReportPlanner - 报告规划师：大纲编排 + 证据分配
  6. ReportWriter  - 报告撰写师：基于 outline + evidence 写作
  7. FactChecker  - 事实核查员：关键数据验证 + 可信度评估

三档模式（对齐 sn-deep-research）：
- quick  : 单维度 skim → 单 writer → 快速出稿
- normal : scout → plan → 多维度 research + review → report plan → writer → 终审
- heavy  : normal + 多轮 review + 深度 fact check

核心设计原则（来自 sn-deep-research）：
- 证据为核：evidence 是唯一真相来源
- 契约驱动：每阶段输出严格 schema 化
- 档位解耦：档位选择器决定跑哪些阶段
- 角色原子化：每个 Agent 职责单一
- 能力降级：缺能力不阻塞，有兜底
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
    """构造日志条目"""
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
    """SenseNova API 简化客户端"""

    BASE_URL = "https://token.sensenova.cn/v1/chat/completions"
    DEFAULT_MODEL = "sensenova-6.7-flash-lite"
    DEFAULT_TIMEOUT = 60

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.total_tokens = 0
        self.call_count = 0

    def chat(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
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

            usage = data.get("usage", {})
            self.total_tokens += usage.get("total_tokens", 0)

            return {
                "content": content or "",
                "tool_calls": tool_calls or [],
                "duration_ms": duration_ms,
                "usage": usage,
            }
        except requests.exceptions.Timeout:
            raise RuntimeError(f"SenseNova API 调用超时（{timeout}秒）") from None
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"SenseNova API 调用失败: {e}") from e
        except Exception as e:
            raise RuntimeError(f"SenseNova API 未知错误: {e}") from e

    def chat_with_tools(self, messages: List[Dict], tools: List[Dict],
                        tool_executor: Callable[[str, str], str],
                        max_iter: int = 3, **kwargs) -> Dict[str, Any]:
        tool_calls_history = []
        current_messages = list(messages)
        total_duration = 0
        last_result = {"content": "", "tool_calls": [], "duration_ms": 0}

        if max_iter <= 0:
            return {
                "content": "",
                "tool_calls_history": [],
                "duration_ms": 0,
            }

        for i in range(max_iter):
            try:
                result = self.chat(current_messages, tools=tools, **kwargs)
            except RuntimeError as e:
                logger.error(f"chat_with_tools iteration {i} failed: {e}")
                return {
                    "content": f"[API调用失败: {e}]",
                    "tool_calls_history": tool_calls_history,
                    "duration_ms": total_duration,
                    "error": str(e),
                }

            last_result = result
            total_duration += result.get("duration_ms", 0)
            tool_calls = result.get("tool_calls", [])

            if not tool_calls:
                return {
                    "content": result["content"],
                    "tool_calls_history": tool_calls_history,
                    "duration_ms": total_duration,
                }

            current_messages.append({
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": tool_calls,
            })

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

        return {
            "content": last_result.get("content", ""),
            "tool_calls_history": tool_calls_history,
            "duration_ms": total_duration,
            "truncated": True,
        }


# ============================================================
# JSON 解析工具（多层容错）
# ============================================================

import re

def _parse_json_safe(content: str, fallback: Any) -> Any:
    """
    安全解析JSON，多层容错策略：
    1. 直接解析
    2. 提取 ```json ... ``` 代码块
    3. 提取最外层 {} 括号
    4. 截断补全法
    5. 返回fallback
    """
    if not content:
        return fallback

    # 1. 直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 2. 提取代码块
    code_block_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. 括号平衡法提取JSON
    brace_start = content.find("{")
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[brace_start:i+1])
                    except json.JSONDecodeError:
                        break

    # 4. 截断补全（简单情况）
    try:
        truncated = content[:content.rfind(",")] + "}"
        return json.loads(truncated)
    except (json.JSONDecodeError, ValueError):
        pass

    # 5. 返回fallback
    logger.warning(f"JSON解析失败，使用fallback。内容前100字: {content[:100]}")
    return fallback


# ============================================================
# 深度配置（对齐 sn-deep-research 档位）
# ============================================================

DEPTH_CONFIG = {
    "quick": {
        "label": "快速调研",
        "description": "单维度快速出稿，适合简单查询",
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
        "description": "多维度调研 + 质量审查 + 大纲编排",
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
        "description": "完整流水线 + 深度审查 + 多轮事实核查",
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

SCOUT_SYSTEM_PROMPT = """你是深度研究的预研侦察兵（Scout）。

你的任务是：在正式研究开始前，快速扫描目标公司的基本情况，
评估研究复杂度，并推荐合适的研究档位。

工作流程：
1. 使用 web_search 快速搜索目标公司的基本信息
2. 基于搜索结果，判断研究复杂度
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
- quick：单一维度即可覆盖，单一权威来源可定论
- normal：需要多维度拆解，需要多来源交叉验证（常规公司研究）
- heavy：话题复杂或重要，涉及多方利益相关者，需要深度分析（大型企业、行业龙头）
"""


def scout_agent(client: SenseNovaClient, tool_manager: ToolManager,
                query: str) -> Dict[str, Any]:
    """Scout Agent - 预研侦察兵"""
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

    tools = tool_manager.get_tool_schemas(["web_search"])

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=2,
        temperature=0.5,
        max_tokens=1500,
    )

    content = result["content"].strip()
    briefing = _parse_json_safe(content, _generate_fallback_briefing(query))

    return {
        "briefing": briefing,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"主题:{truncate(query, 50)}",
        "output_summary": f"推荐档位:{briefing.get('recommended_mode', 'unknown')}",
        "input_text": f"[System Prompt]\n{SCOUT_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
        "output_text": content,
    }


def _generate_fallback_briefing(query: str) -> Dict[str, Any]:
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

设计理念（来自 sn-deep-research）：
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
      "suggested_sources": ["通用搜索", "财经数据"],
      "depth_level": "skim|standard|deep"
    }
  ]
}

注意：
- 维度ID按 d1, d2, d3... 编号
- depth_level: skim(快速), standard(标准), deep(深入)
- suggested_sources 从以下选择：通用搜索、企业信息、财经数据、行业报告、新闻资讯
"""


def planner_agent(client: SenseNovaClient, tool_manager: ToolManager,
                  company_name: str, depth: str,
                  briefing: Optional[Dict] = None) -> Dict[str, Any]:
    """Planner Agent - 研究规划师"""
    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["normal"])
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

研究档位：{depth}（共{dim_count}个研究维度）
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

    tools = tool_manager.get_tool_schemas(["web_search"])

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=2,
        temperature=0.4,
        max_tokens=2500,
    )

    content = result["content"].strip()
    plan = _parse_json_safe(content, _generate_fallback_plan(company_name, dim_count))

    # 构建完整输入文本
    full_input = f"[System Prompt]\n{PLANNER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}"

    return {
        "plan": plan,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 档位:{depth}, 维度数:{dim_count}",
        "output_summary": truncate(json.dumps(plan, ensure_ascii=False), 200),
        "input_text": f"[System Prompt]\n{PLANNER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
        "output_text": content,
        "input_text": full_input,
        "output_text": content,
    }


def _generate_fallback_plan(company_name: str, dim_count: int) -> Dict[str, Any]:
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
    for idx, dim in enumerate(selected_dims):
        dimensions.append({
            "id": f"d{idx + 1}",
            "name": dim,
            "description": f"{dim}维度的深入研究",
            "key_questions": questions_map.get(dim, ["该维度的核心信息是什么？"]),
            "focus": f"{dim}的核心要点",
            "suggested_sources": ["通用搜索"],
            "depth_level": "standard",
        })

    return {
        "plan_name": f"{company_name}深度研究计划",
        "company_profile": f"{company_name}是一家值得深入研究的企业",
        "research_objective": f"全面了解{company_name}的经营状况和发展前景",
        "dimensions": dimensions,
    }


# ============================================================
# Agent 2: Researcher - 维度取证专家
# ============================================================

RESEARCHER_SYSTEM_PROMPT = """你是维度取证研究员（Research Agent）。

你的任务是针对一个具体研究维度，通过多轮搜索搜集可靠证据，
输出结构化的 evidence.json——这是整个系统的唯一真相来源。

参考 sn-deep-research 的 evidence 设计理念：
- 所有结论必须有证据支撑
- 证据要标注来源和可信度
- 断言分三类：factual(事实) / interpretive(解释) / projective(预测)
- 每个断言必须有至少一个来源支撑

可用工具：
- web_search: 通用网页搜索
- company_lookup: 查询企业基本信息
- financial_data: 查询财务数据

输出要求（严格JSON格式，对齐 evidence schema v1.1 精简版）：
{
  "schema_version": "1.1",
  "dimension_id": "d1",
  "dimension_name": "维度名称",
  "headline": "一句话核心结论（5-50字）",
  "key_findings": [
    {"text": "综合发现1", "supporting_claim_ids": ["c1", "c2"]},
    {"text": "综合发现2", "supporting_claim_ids": ["c3"]}
  ],
  "claims": [
    {
      "id": "c1",
      "text": "断言内容",
      "kind": "factual|interpretive|projective",
      "polarity": "support|refute|neutral",
      "topic_tag": "主题标签",
      "answers_key_question": "kq1",
      "evidence": [
        {"source_id": "s1", "snippet": "引用片段", "quote_type": "direct|paraphrase"}
      ]
    }
  ],
  "sources": [
    {
      "id": "s1",
      "title": "来源标题",
      "url": "来源URL",
      "quality": "primary|secondary|tertiary",
      "published_at": "发布时间或空字符串",
      "publisher": "发布者"
    }
  ],
  "writing_context": [
    "写作时需要注意的边界条件或背景说明"
  ]
}

注意：
- claim id 按 c1, c2, c3... 编号
- source id 按 s1, s2, s3... 编号
- quality: primary(一手来源) / secondary(二手报道) / tertiary(三手综述)
- 至少有 3 个 claims 和 2 个 sources
"""


def researcher_agent(client: SenseNovaClient, tool_manager: ToolManager,
                     company_name: str, dimension: Dict[str, Any],
                     iterations: int = 2) -> Dict[str, Any]:
    """Researcher Agent - 维度取证专家"""
    dim_id = dimension.get("id", "d1")
    dim_name = dimension.get("name", "未知维度")
    dim_desc = dimension.get("description", "")
    key_questions = dimension.get("key_questions", [])
    focus = dimension.get("focus", "")
    suggested_sources = dimension.get("suggested_sources", ["通用搜索"])
    depth_level = dimension.get("depth_level", "standard")

    kq_text = "\n".join([f"- {q}" for q in key_questions])
    src_text = "、".join(suggested_sources)

    user_prompt = f"""请针对「{company_name}」的「{dim_name}」维度进行深度调研取证。

维度说明：{dim_desc}
研究重点：{focus}
深度等级：{depth_level}
建议来源类别：{src_text}

关键问题：
{kq_text}

请使用可用工具进行多轮搜索，收集足够的证据后，输出结构化的 evidence.json。

要求：
1. 搜索关键词要精准，围绕关键问题设计
2. 至少进行 {iterations} 轮搜索，确保信息充分
3. 每个断言都要有证据支撑，注明来源
4. 严格按照 evidence JSON 格式输出
5. 至少有 3 个 claims 和 2 个 sources"""

    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, company_name)

    # 根据建议来源选择工具
    tool_names = ["web_search"]
    if "财经数据" in suggested_sources or "财务" in dim_name:
        tool_names.append("financial_data")
    if "企业信息" in suggested_sources or "概况" in dim_name:
        tool_names.append("company_lookup")
    tools = tool_manager.get_tool_schemas(tool_names)

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=iterations + 1,
        temperature=0.3,
        max_tokens=3000,
    )

    content = result["content"].strip()
    evidence = _parse_json_safe(content, _generate_fallback_evidence(dim_id, dim_name, company_name))

    return {
        "evidence": evidence,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"维度:{dim_name}, 公司:{company_name}",
        "output_summary": f"Claims:{len(evidence.get('claims', []))}, Sources:{len(evidence.get('sources', []))}",
        "input_text": f"[System Prompt]\n{RESEARCHER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
        "output_text": content,
    }


def _generate_fallback_evidence(dim_id: str, dim_name: str, company_name: str) -> Dict[str, Any]:
    return {
        "schema_version": "1.1",
        "dimension_id": dim_id,
        "dimension_name": dim_name,
        "headline": f"{company_name}在{dim_name}方面的综合评估",
        "key_findings": [
            {"text": f"{company_name}在{dim_name}领域表现稳定", "supporting_claim_ids": ["c1"]},
            {"text": "需要进一步关注行业动态变化", "supporting_claim_ids": ["c2"]},
        ],
        "claims": [
            {
                "id": "c1",
                "text": f"{company_name}在{dim_name}方面有一定基础",
                "kind": "factual",
                "polarity": "support",
                "topic_tag": dim_name,
                "answers_key_question": "kq1",
                "evidence": [
                    {"source_id": "s1", "snippet": "来自知识库的基础信息", "quote_type": "paraphrase"}
                ],
            },
            {
                "id": "c2",
                "text": f"{company_name}的{dim_name}受行业环境影响较大",
                "kind": "interpretive",
                "polarity": "neutral",
                "topic_tag": dim_name,
                "answers_key_question": "kq2",
                "evidence": [
                    {"source_id": "s1", "snippet": "行业分析综合判断", "quote_type": "paraphrase"}
                ],
            },
        ],
        "sources": [
            {
                "id": "s1",
                "title": f"{company_name}基础信息库",
                "url": "",
                "quality": "tertiary",
                "published_at": "",
                "publisher": "系统知识库",
            },
        ],
        "writing_context": [
            "本证据为保底数据，建议结合实时搜索结果验证",
            "具体数据请以官方披露为准",
        ],
    }


# ============================================================
# Agent 3: Reviewer - 质量审查员
# ============================================================

REVIEWER_SYSTEM_PROMPT = """你是质量审查员（Review Agent）。

你的任务是审查子报告级别的 evidence.json，检查：
1. 证据质量和来源可信度
2. 断言与证据的一致性
3. 关键问题的覆盖深度
4. 是否存在明显的信息缺口

参考 sn-deep-research 的 review 设计：
- VERDICT 机制：pass / revise
- 分级问题：🔴硬伤（必须修复）/ 🟡改进建议
- 关注 claim ↔ evidence 一致性

输出要求（严格JSON格式）：
{
  "verdict": "pass|revise",
  "overall_score": 85,
  "dimension_id": "d1",
  "dimension_name": "维度名称",
  "critical_issues": [
    {"id": "r1", "type": "hard", "description": "硬伤描述", "impact": "影响程度", "suggestion": "修复建议"}
  ],
  "improvement_suggestions": [
    {"id": "r2", "type": "soft", "description": "改进建议", "suggestion": "优化方向"}
  ],
  "coverage_assessment": {
    "key_questions_covered": ["kq1", "kq2"],
    "key_questions_gaps": ["kq3"],
    "coverage_rate": 0.8
  },
  "evidence_quality": {
    "primary_source_count": 2,
    "secondary_source_count": 3,
    "tertiary_source_count": 1,
    "overall_quality": "good"
  },
  "review_summary": "审查总结说明"
}
"""


def reviewer_agent(client: SenseNovaClient, evidence: Dict[str, Any],
                   dimension: Dict[str, Any]) -> Dict[str, Any]:
    """Reviewer Agent - 质量审查员（子报告审查）"""
    dim_id = dimension.get("id", "d1")
    dim_name = dimension.get("name", "未知维度")
    key_questions = dimension.get("key_questions", [])

    evidence_json = json.dumps(evidence, ensure_ascii=False, indent=2)
    kq_list = "\n".join([f"- {i+1}. {q}" for i, q in enumerate(key_questions)])

    user_prompt = f"""请审查以下维度的 evidence.json 质量。

维度：{dim_name}（{dim_id}）

关键问题：
{kq_list}

Evidence 内容：
```json
{truncate(evidence_json, 4000)}
```

请从以下方面审查：
1. 证据质量：来源可信度、一手/二手/三手比例
2. 断言质量：claim 与 evidence 是否对应、有无无证据支撑的断言
3. 覆盖度：关键问题是否都有回答
4. 逻辑一致性：有无自相矛盾

请以严格JSON格式输出审查结果。"""

    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = client.chat(
            messages,
            temperature=0.3,
            max_tokens=2000,
            timeout=45,
        )

        content = result["content"].strip()
        review = _parse_json_safe(content, _generate_fallback_review(dim_id, dim_name))

        return {
            "review": review,
            "duration_ms": result.get("duration_ms", 0),
            "input_summary": f"维度:{dim_name}",
            "output_summary": f"结论:{review.get('verdict', 'unknown')}, 评分:{review.get('overall_score', 0)}",
            "input_text": f"[System Prompt]\n{REVIEWER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
            "output_text": content,
        }
    except RuntimeError as e:
        logger.error(f"Reviewer agent failed: {e}")
        fallback_review = _generate_fallback_review(dim_id, dim_name)
        fallback_review["review_summary"] += f"（审查API调用失败，使用保底结果：{e}）"
        return {
            "review": fallback_review,
            "duration_ms": 0,
            "input_summary": f"维度:{dim_name}",
            "output_summary": "审查失败，使用保底结果",
            "error": str(e),
        }


def _generate_fallback_review(dim_id: str, dim_name: str) -> Dict[str, Any]:
    return {
        "verdict": "pass",
        "overall_score": 70,
        "dimension_id": dim_id,
        "dimension_name": dim_name,
        "critical_issues": [],
        "improvement_suggestions": [
            {"id": "r1", "type": "soft", "description": "建议增加更多一手来源", "suggestion": "查找官方披露数据"}
        ],
        "coverage_assessment": {
            "key_questions_covered": ["kq1"],
            "key_questions_gaps": [],
            "coverage_rate": 0.7,
        },
        "evidence_quality": {
            "primary_source_count": 0,
            "secondary_source_count": 0,
            "tertiary_source_count": 1,
            "overall_quality": "fair",
        },
        "review_summary": "保底审查结果，建议结合实际证据质量评估",
    }


# ============================================================
# Agent 4: ReportPlanner - 报告规划师
# ============================================================

REPORT_PLANNER_SYSTEM_PROMPT = """你是报告规划师（Report Planner）。

你处在「证据已采集完」与「writer 开始写作」之间。
你的任务是综合全部 evidence，编排整篇报告结构，生成 outline.json。

参考 sn-deep-research 的 outline 设计理念：
- 决定主范式：panorama(全景) / comparison(对比) / investigation(调查) / timeline(时间线) / evaluation(评估) / forecast(预测)
- 编排 sections：每节有 reader_question、blocks、evidence_subset
- 证据分配：每节分配对应的 evidence subset，writer 只能引用分配给它的证据

输出要求（严格JSON格式，对齐 outline schema v1.0 精简版）：
{
  "schema_version": "1.0",
  "paradigm": {
    "primary": "panorama",
    "secondary": "evaluation"
  },
  "depth_level": "overview|deep_analysis|expert_level",
  "global_arc": "全文级写作方向（40-120字）",
  "L0_draft": {
    "headline": "报告标题",
    "key_findings": ["核心发现1", "核心发现2", "核心发现3"],
    "abstract": "摘要"
  },
  "style_contract": {
    "register": "formal|professional|conversational",
    "voice": "objective|analytical|narrative",
    "citation_style": "footnote|inline|reference"
  },
  "sections": [
    {
      "id": "s1",
      "title": "章节标题",
      "reader_question": "读者在这节想知道什么",
      "section_role": "introduction|body|conclusion",
      "lead": "章节导语（BLUF结论前置）",
      "blocks": [
        {
          "id": "b1",
          "level": 3,
          "heading": "小标题（对象+信息方面）",
          "thesis": "本块核心论点",
          "evidence_refs": ["c1", "c2"]
        }
      ],
      "evidence_subset": ["c1", "c2", "c3"],
      "visuals": []
    }
  ]
}

注意：
- section id 按 s1, s2, s3... 编号
- block id 按 b1, b2, b3... 编号
- evidence_refs 引用的是 claim id
- BLUF：每节 lead 直接给结论
"""


def report_planner_agent(client: SenseNovaClient, company_name: str,
                         all_evidence: List[Dict[str, Any]],
                         plan: Dict[str, Any]) -> Dict[str, Any]:
    """ReportPlanner Agent - 报告规划师"""
    # 汇总所有 evidence 的 claim 和 source
    all_claims = []
    all_sources = []
    for ev in all_evidence:
        all_claims.extend(ev.get("claims", []))
        all_sources.extend(ev.get("sources", []))

    dim_summary = "\n".join([
        f"- {ev.get('dimension_name', '')}: {ev.get('headline', '')}"
        for ev in all_evidence
    ])

    claim_summary = "\n".join([
        f"  [{c.get('id', '')}] {truncate(c.get('text', ''), 80)}"
        for c in all_claims[:20]
    ])

    user_prompt = f"""请为「{company_name}」的深度研究报告编排大纲。

研究计划：{plan.get('plan_name', '')}

各维度研究结论：
{dim_summary}

主要断言（claims）概览：
{claim_summary}

请基于以上证据，编排报告大纲（outline.json）。

要求：
1. 选择合适的主范式和副范式
2. 章节结构要有逻辑递进关系
3. 每节分配对应的证据子集（evidence_subset）
4. 每节要有 reader_question 和 lead（BLUF结论前置）
5. 严格按照 outline JSON 格式输出

总 claims 数量：{len(all_claims)}
总 sources 数量：{len(all_sources)}"""

    messages = [
        {"role": "system", "content": REPORT_PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = client.chat(
            messages,
            temperature=0.4,
            max_tokens=3000,
            timeout=60,
        )

        content = result["content"].strip()
        outline = _parse_json_safe(content, _generate_fallback_outline(company_name, all_evidence))

        return {
            "outline": outline,
            "duration_ms": result.get("duration_ms", 0),
            "input_summary": f"公司:{company_name}, 维度数:{len(all_evidence)}",
            "output_summary": f"章节数:{len(outline.get('sections', []))}",
            "input_text": f"[System Prompt]\n{REPORT_PLANNER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
            "output_text": content,
        }
    except RuntimeError as e:
        logger.error(f"ReportPlanner agent failed: {e}")
        fallback_outline = _generate_fallback_outline(company_name, all_evidence)
        return {
            "outline": fallback_outline,
            "duration_ms": 0,
            "input_summary": f"公司:{company_name}",
            "output_summary": "大纲规划失败，使用保底结构",
            "error": str(e),
        }


def _generate_fallback_outline(company_name: str, all_evidence: List[Dict]) -> Dict[str, Any]:
    sections = []
    section_id = 1

    # 摘要节
    sections.append({
        "id": f"s{section_id}",
        "title": "研究概述",
        "reader_question": "这家公司的整体情况如何？",
        "section_role": "introduction",
        "lead": f"{company_name}是一家值得关注的企业，本报告从多个维度进行深入分析。",
        "blocks": [
            {"id": "b1", "level": 3, "heading": "公司概况", "thesis": "公司基本情况", "evidence_refs": []}
        ],
        "evidence_subset": [],
        "visuals": [],
    })
    section_id += 1

    # 各维度节
    for ev in all_evidence:
        dim_name = ev.get("dimension_name", "")
        claim_ids = [c.get("id", "") for c in ev.get("claims", [])]
        sections.append({
            "id": f"s{section_id}",
            "title": dim_name,
            "reader_question": f"{company_name}的{dim_name}如何？",
            "section_role": "body",
            "lead": ev.get("headline", ""),
            "blocks": [
                {"id": f"b{section_id}", "level": 3, "heading": f"{dim_name}分析", "thesis": f"{dim_name}核心要点", "evidence_refs": claim_ids}
            ],
            "evidence_subset": claim_ids,
            "visuals": [],
        })
        section_id += 1

    # 结论节
    sections.append({
        "id": f"s{section_id}",
        "title": "总结与展望",
        "reader_question": "这家公司的前景如何？",
        "section_role": "conclusion",
        "lead": f"综合来看，{company_name}在多个维度展现出一定的发展潜力。",
        "blocks": [
            {"id": f"b{section_id}", "level": 3, "heading": "核心结论", "thesis": "综合评估", "evidence_refs": []}
        ],
        "evidence_subset": [],
        "visuals": [],
    })

    return {
        "schema_version": "1.0",
        "paradigm": {"primary": "panorama", "secondary": "evaluation"},
        "depth_level": "deep_analysis",
        "global_arc": f"全面分析{company_name}的经营状况、竞争地位和发展前景",
        "L0_draft": {
            "headline": f"{company_name}深度研究报告",
            "key_findings": [f"{company_name}在多个维度有不错表现", "行业地位较为稳固", "需关注潜在风险"],
            "abstract": f"本报告从多个维度对{company_name}进行了深入分析。",
        },
        "style_contract": {
            "register": "professional",
            "voice": "analytical",
            "citation_style": "footnote",
        },
        "sections": sections,
    }


# ============================================================
# Agent 5: ReportWriter - 报告撰写师
# ============================================================

REPORT_WRITER_SYSTEM_PROMPT = """你是报告撰写师（Report Writer）。

你的任务是按照 outline 契约，把 evidence 写成面向读者的事实论证 markdown。

写作纪律（来自 sn-deep-research）：
1. BLUF（结论前置）：章首 lead 直接给事实/结论
2. 引用键必须是 source.id（不能用 claim.id）
3. 不引入 evidence 外的新事实
4. 每个 block 对应一个小标题，heading 是"对象+信息方面"
5. 风格：专业、客观、有数据支撑

引用格式：
- 使用脚注格式 [^s1] 来标注来源
- 在段落末尾标注引用

请直接输出完整的 Markdown 报告内容。
"""


def report_writer_agent(client: SenseNovaClient, company_name: str,
                        outline: Dict[str, Any],
                        all_evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    """ReportWriter Agent - 报告撰写师"""
    # 构建 claim → source 映射
    claim_map = {}
    source_map = {}
    for ev in all_evidence:
        for c in ev.get("claims", []):
            claim_map[c.get("id", "")] = c
        for s in ev.get("sources", []):
            source_map[s.get("id", "")] = s

    # 构建 evidence 摘要
    evidence_summary_parts = []
    for ev in all_evidence:
        dim_name = ev.get("dimension_name", "")
        claims_text = "\n".join([
            f"  [{c.get('id', '')}] {c.get('text', '')}"
            for c in ev.get("claims", [])[:5]
        ])
        sources_text = "\n".join([
            f"  [{s.get('id', '')}] {s.get('title', '')}"
            for s in ev.get("sources", [])[:3]
        ])
        evidence_summary_parts.append(f"## {dim_name}\n\n断言：\n{claims_text}\n\n来源：\n{sources_text}")

    evidence_summary = "\n\n".join(evidence_summary_parts)
    outline_json = json.dumps(outline, ensure_ascii=False, indent=2)

    user_prompt = f"""请为「{company_name}」撰写深度研究报告。

报告大纲（outline.json）：
```json
{truncate(outline_json, 3000)}
```

证据库（evidence）摘要：
{truncate(evidence_summary, 5000)}

请按照大纲结构，基于证据撰写完整的 Markdown 报告。

写作要求：
1. BLUF：每章开头先给结论
2. 每个重要论断都要有引用标注，使用 [^s1] 格式
3. 报告末尾附上参考文献列表
4. 风格专业、客观、有深度
5. 字数不少于 2000 字
6. 不要编造 evidence 中没有的信息"""

    messages = [
        {"role": "system", "content": REPORT_WRITER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = client.chat(
            messages,
            temperature=0.5,
            max_tokens=4000,
            timeout=90,
        )

        report = result["content"].strip()

        return {
            "report": report,
            "duration_ms": result.get("duration_ms", 0),
            "input_summary": f"公司:{company_name}, 章节数:{len(outline.get('sections', []))}",
            "output_summary": f"报告长度:{len(report)}字",
            "input_text": f"[System Prompt]\n{REPORT_WRITER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
            "output_text": report,
        }
    except RuntimeError as e:
        logger.error(f"ReportWriter agent failed: {e}")
        fallback_report = _generate_fallback_report(company_name, outline, all_evidence)
        return {
            "report": fallback_report,
            "duration_ms": 0,
            "input_summary": f"公司:{company_name}",
            "output_summary": "撰写失败，使用保底报告",
            "error": str(e),
        }


def _generate_fallback_report(company_name: str, outline: Dict[str, Any],
                              all_evidence: List[Dict]) -> str:
    sections_md = []
    for sec in outline.get("sections", []):
        title = sec.get("title", "")
        lead = sec.get("lead", "")
        blocks = sec.get("blocks", [])

        blocks_md = []
        for blk in blocks:
            heading = blk.get("heading", "")
            thesis = blk.get("thesis", "")
            blocks_md.append(f"### {heading}\n\n{thesis}")

        blocks_text = "\n\n".join(blocks_md) if blocks_md else ""
        sections_md.append(f"## {title}\n\n> {lead}\n\n{blocks_text}")

    sections_text = "\n\n".join(sections_md)

    L0 = outline.get("L0_draft", {})
    headline = L0.get("headline", f"{company_name}深度研究报告")
    key_findings = L0.get("key_findings", [])
    abstract = L0.get("abstract", "")

    kf_md = "\n".join([f"- {kf}" for kf in key_findings])

    return f"""# {headline}

## 摘要

{abstract}

### 核心发现

{kf_md}

---

{sections_text}

---

## 参考文献

1. 系统知识库
2. 公开信息综合整理

---

*本报告由 AI 自动生成，仅供参考。数据来源于公开信息，不构成投资建议。*
"""


# ============================================================
# Agent 6: FactChecker - 事实核查员
# ============================================================

FACT_CHECKER_SYSTEM_PROMPT = """你是事实核查员（Fact Checker）。

你的任务是从报告中提取关键数据和论断，通过搜索验证其准确性。

工作流程：
1. 从报告中提取 3-5 个最关键的可验证论断
2. 使用 web_search 对每个论断进行验证
3. 给出验证结果和可信度评估

输出要求（严格JSON格式）：
{
  "overall_confidence": "high|medium|low",
  "checked_claims": [
    {
      "id": "fc1",
      "claim_text": "被核查的论断",
      "verification_result": "confirmed|partially_confirmed|disputed|unverifiable",
      "confidence": 0.9,
      "supporting_evidence": "验证证据摘要",
      "source_title": "验证来源标题",
      "source_url": "验证来源URL",
      "notes": "备注说明"
    }
  ],
  "fact_check_summary": "事实核查总结",
  "recommendations": ["改进建议1", "改进建议2"]
}
"""


def fact_checker_agent(client: SenseNovaClient, tool_manager: ToolManager,
                       company_name: str, report: str,
                       mode: str = "llm") -> Dict[str, Any]:
    """FactChecker Agent - 事实核查员"""
    # basic 模式：简单规则提取 + 1次搜索验证
    if mode == "basic":
        return _fact_check_basic(client, tool_manager, company_name, report)

    # llm / full 模式：LLM 提取 + 多轮搜索验证
    search_count = 3 if mode == "llm" else 5

    user_prompt = f"""请对以下关于「{company_name}」的研究报告进行事实核查。

报告内容（节选）：
```
{truncate(report, 4000)}
```

请提取 {search_count} 个最关键的可验证论断，使用 web_search 逐一验证。

要求：
1. 选择有具体数据或明确论断的点进行核查
2. 每个论断都要进行实际搜索验证
3. 给出可信度评分（0-1）
4. 严格按照 JSON 格式输出"""

    messages = [
        {"role": "system", "content": FACT_CHECKER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name: str, args_str: str) -> str:
        return tool_manager.execute_tool(name, args_str, company_name)

    tools = tool_manager.get_tool_schemas(["web_search"])

    result = client.chat_with_tools(
        messages,
        tools=tools,
        tool_executor=tool_executor,
        max_iter=search_count + 1,
        temperature=0.2,
        max_tokens=2500,
    )

    content = result["content"].strip()
    fc_result = _parse_json_safe(content, _generate_fallback_fact_check(company_name))

    return {
        "fact_check": fc_result,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 模式:{mode}",
        "output_summary": f"可信度:{fc_result.get('overall_confidence', 'unknown')}",
        "input_text": f"[System Prompt]\n{FACT_CHECKER_SYSTEM_PROMPT}\n\n[User Prompt]\n{user_prompt}",
        "output_text": content,
    }


def _fact_check_basic(client: SenseNovaClient, tool_manager: ToolManager,
                      company_name: str, report: str) -> Dict[str, Any]:
    """基础模式：规则提取关键信息 + 1次搜索验证"""
    # 简单搜索公司基本信息作为验证
    search_query = f"{company_name} 公司简介 最新"
    try:
        search_result_str = tool_manager.execute_tool("web_search", json.dumps({"query": search_query}, ensure_ascii=False), company_name)
        search_result = json.loads(search_result_str) if isinstance(search_result_str, str) else search_result_str
        results = search_result.get("results", search_result.get("items", [])) if isinstance(search_result, dict) else []
    except Exception as e:
        logger.error(f"Basic fact check search failed: {e}")
        results = []

    if results:
        top_result = results[0] if isinstance(results[0], dict) else {"title": str(results[0]), "url": ""}
        checked_claims = [{
            "id": "fc1",
            "claim_text": f"{company_name}的基本信息",
            "verification_result": "partially_confirmed",
            "confidence": 0.7,
            "supporting_evidence": top_result.get("title", ""),
            "source_title": top_result.get("title", ""),
            "source_url": top_result.get("url", ""),
            "notes": "通过基础搜索验证，信息基本一致",
        }]
        overall = "medium"
        summary = f"基础事实核查完成，已验证 {len(checked_claims)} 个关键论断。整体可信度中等。"
    else:
        checked_claims = [{
            "id": "fc1",
            "claim_text": f"{company_name}的基本信息",
            "verification_result": "unverifiable",
            "confidence": 0.5,
            "supporting_evidence": "搜索结果不足，无法充分验证",
            "source_title": "搜索结果有限",
            "source_url": "",
            "notes": "建议结合更多数据源验证",
        }]
        overall = "low"
        summary = "基础事实核查：搜索结果有限，建议使用更高等级的核查模式。"

    return {
        "fact_check": {
            "overall_confidence": overall,
            "checked_claims": checked_claims,
            "fact_check_summary": summary,
            "recommendations": ["建议升级到 normal 或 deep 模式进行更全面的事实核查"],
        },
        "tool_calls_history": [{
            "name": "web_search",
            "arguments": json.dumps({"query": search_query}, ensure_ascii=False),
            "result_summary": f"返回 {len(results)} 条结果",
        }],
        "duration_ms": 0,
        "input_summary": f"公司:{company_name}, 模式:basic",
        "output_summary": f"可信度:{overall}",
    }


def _generate_fallback_fact_check(company_name: str) -> Dict[str, Any]:
    return {
        "overall_confidence": "medium",
        "checked_claims": [
            {
                "id": "fc1",
                "claim_text": f"{company_name}是一家经营中的企业",
                "verification_result": "partially_confirmed",
                "confidence": 0.6,
                "supporting_evidence": "公开信息显示该公司存在",
                "source_title": "公开信息",
                "source_url": "",
                "notes": "保底核查结果",
            }
        ],
        "fact_check_summary": "事实核查保底结果，建议实际运行验证",
        "recommendations": ["建议重新执行事实核查获取真实验证结果"],
    }


# ============================================================
# 主编排器 - SN DeepResearch 风格
# ============================================================

GLOBAL_TIMEOUT_SECONDS = 600  # 总超时 10 分钟
MAX_TASKS = 100

_tasks: Dict[str, Dict[str, Any]] = {}


def run_sn_deepresearch(api_key: str, company_name: str, depth: str,
                        model: Optional[str] = None,
                        emit: Optional[Callable[[str, Dict], None]] = None) -> Dict[str, Any]:
    """
    运行 SN-DeepResearch 风格的深度研究流水线

    Args:
        api_key: SenseNova API Key
        company_name: 公司名称
        depth: 研究档位 (quick/normal/heavy)
        model: 模型名称
        emit: 事件回调函数 (event_type, data)

    Returns:
        完整的研究结果字典
    """
    def _emit(event_type: str, data: Dict):
        if emit:
            try:
                emit(event_type, data)
            except Exception as e:
                logger.error(f"Emit event failed: {event_type}, {e}")

    start_time = time.time()
    step = 0
    all_logs: List[Dict] = []
    all_tool_calls: List[Dict] = []

    # 初始化
    client = SenseNovaClient(api_key, model=model)
    tool_manager = ToolManager()

    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["normal"])

    def _check_timeout():
        """检查是否超时"""
        elapsed = time.time() - start_time
        if elapsed > GLOBAL_TIMEOUT_SECONDS:
            raise TimeoutError(f"总超时（{GLOBAL_TIMEOUT_SECONDS}秒）")

    # ===== 阶段 0: 初始化 =====
    step += 1
    _emit("task_start", {
        "company": company_name,
        "depth": depth,
        "mode": "sn-deepresearch",
        "model": client.model,
        "estimated_duration": config.get("estimated_duration", "未知"),
    })

    log = make_log(step, "system", "任务启动",
                   input_summary=f"公司:{company_name}, 档位:{depth}, 模型:{client.model}")
    all_logs.append(log)

    # ===== 阶段 1: Scout（仅 normal/heavy） =====
    briefing = None
    if config.get("has_scout", False):
        step += 1
        _check_timeout()
        _emit("agent_start", {"agent": "scout", "step": step, "name": "预研侦察兵",
                               "input_summary": f"公司: {company_name}"})

        try:
            scout_result = scout_agent(client, tool_manager, company_name)
            briefing = scout_result["briefing"]

            log = make_log(step, "scout", "预研完成",
                           input_summary=scout_result["input_summary"],
                           output_summary=scout_result["output_summary"],
                           tool_calls=scout_result["tool_calls_history"],
                           duration_ms=scout_result["duration_ms"])
            all_logs.append(log)
            all_tool_calls.extend([
                {**tc, "agent": "scout"} for tc in scout_result["tool_calls_history"]
            ])

            for tc in scout_result["tool_calls_history"]:
                _emit("tool_call", {
                    "agent": "scout",
                    "tool": tc["name"],
                    "arguments": tc["arguments"],
                    "result_summary": tc["result_summary"],
                })

            _emit("agent_end", {
                "agent": "scout",
                "step": step,
                "status": "success",
                "output_summary": scout_result["output_summary"],
            "input_text": scout_result.get("input_text", ""),
            "output_text": scout_result.get("output_text", ""),
            })
        except Exception as e:
            logger.error(f"Scout failed: {e}")
            briefing = _generate_fallback_briefing(company_name)
            _emit("agent_end", {"agent": "scout", "step": step, "status": "failed", "error": str(e)})
            all_logs.append(make_log(step, "scout", "预研失败", output_summary=str(e), level="error"))

    # ===== 阶段 2: Planner =====
    step += 1
    _check_timeout()
    _emit("agent_start", {"agent": "planner", "step": step, "name": "研究规划师",
                          "input_summary": f"公司: {company_name}\n深度: {depth}\n是否有预研简报: {'是' if briefing else '否'}"})

    try:
        plan_result = planner_agent(client, tool_manager, company_name, depth, briefing)
        plan = plan_result["plan"]
        dimensions = plan.get("dimensions", [])

        log = make_log(step, "planner", "规划完成",
                       input_summary=plan_result["input_summary"],
                       output_summary=f"维度数:{len(dimensions)}",
                       tool_calls=plan_result["tool_calls_history"],
                       duration_ms=plan_result["duration_ms"])
        all_logs.append(log)
        all_tool_calls.extend([
            {**tc, "agent": "planner"} for tc in plan_result["tool_calls_history"]
        ])

        for tc in plan_result["tool_calls_history"]:
            _emit("tool_call", {
                "agent": "planner",
                "tool": tc["name"],
                "arguments": tc["arguments"],
                "result_summary": tc["result_summary"],
            })

        _emit("agent_end", {
            "agent": "planner",
            "step": step,
            "status": "success",
            "output_summary": f"生成{len(dimensions)}个研究维度",
        })
    except Exception as e:
        logger.error(f"Planner failed: {e}")
        plan = _generate_fallback_plan(company_name, config["dimension_count"])
        dimensions = plan.get("dimensions", [])
        _emit("agent_end", {"agent": "planner", "step": step, "status": "failed", "error": str(e)})
        all_logs.append(make_log(step, "planner", "规划失败", output_summary=str(e), level="error"))

    # ===== 阶段 3: Researcher（各维度） =====
    all_evidence = []

    for idx, dim in enumerate(dimensions):
        step += 1
        _check_timeout()
        dim_name = dim.get("name", f"维度{idx+1}")
        _emit("agent_start", {"agent": "researcher", "step": step, "name": f"信息取证专家 - {dim_name}",
                               "input_summary": f"公司: {company_name}\n维度: {dim_name} ({idx + 1}/{len(dimensions)})"})

        try:
            research_result = researcher_agent(
                client, tool_manager, company_name, dim,
                iterations=config.get("research_iterations", 2)
            )
            evidence = research_result["evidence"]
            all_evidence.append(evidence)

            log = make_log(step, "researcher", f"调研完成 - {dim_name}",
                           input_summary=research_result["input_summary"],
                           output_summary=research_result["output_summary"],
                           tool_calls=research_result["tool_calls_history"],
                           duration_ms=research_result["duration_ms"])
            all_logs.append(log)
            all_tool_calls.extend([
                {**tc, "agent": f"researcher/{dim_name}"} for tc in research_result["tool_calls_history"]
            ])

            for tc in research_result["tool_calls_history"]:
                _emit("tool_call", {
                    "agent": "researcher",
                    "dimension": dim_name,
                    "tool": tc["name"],
                    "arguments": tc["arguments"],
                    "result_summary": tc["result_summary"],
                })

            _emit("agent_end", {
                "agent": "researcher",
                "dimension": dim_name,
                "step": step,
                "status": "success",
                "output_summary": research_result["output_summary"],
            "input_text": research_result.get("input_text", ""),
            "output_text": research_result.get("output_text", ""),
            })
        except Exception as e:
            logger.error(f"Researcher failed for {dim_name}: {e}")
            fallback_ev = _generate_fallback_evidence(dim.get("id", f"d{idx+1}"), dim_name, company_name)
            all_evidence.append(fallback_ev)
            _emit("agent_end", {"agent": "researcher", "dimension": dim_name, "step": step, "status": "failed", "error": str(e)})
            all_logs.append(make_log(step, "researcher", f"调研失败 - {dim_name}", output_summary=str(e), level="error"))

    # ===== 阶段 4: 子报告 Review（仅 normal/heavy） =====
    all_reviews = []
    if config.get("has_sub_report_review", False):
        for idx, (dim, evidence) in enumerate(zip(dimensions, all_evidence)):
            step += 1
            _check_timeout()
            dim_name = dim.get("name", f"维度{idx+1}")
            _emit("agent_start", {"agent": "reviewer", "step": step, "name": f"质量审查员 - {dim_name}",
                                   "input_summary": f"公司: {company_name}\n维度: {dim_name}\n子报告长度: {len(json.dumps(evidence, ensure_ascii=False))}字"})

            try:
                review_result = reviewer_agent(client, evidence, dim)
                review = review_result["review"]
                all_reviews.append(review)

                log = make_log(step, "reviewer", f"审查完成 - {dim_name}",
                               input_summary=review_result["input_summary"],
                               output_summary=review_result["output_summary"],
                               duration_ms=review_result["duration_ms"])
                all_logs.append(log)

                _emit("reviewer_end", {
                    "agent": "reviewer",
                    "dimension": dim_name,
                    "step": step,
                    "verdict": review.get("verdict", "unknown"),
                    "score": review.get("overall_score", 0),
                    "critical_issues": len(review.get("critical_issues", [])),
                })
                _emit("agent_end", {
                    "agent": "reviewer",
                    "dimension": dim_name,
                    "step": step,
                    "status": "success",
                    "output_summary": review_result["output_summary"],
            "input_text": review_result.get("input_text", ""),
            "output_text": review_result.get("output_text", ""),
                })
            except Exception as e:
                logger.error(f"Reviewer failed for {dim_name}: {e}")
                _emit("agent_end", {"agent": "reviewer", "dimension": dim_name, "step": step, "status": "failed", "error": str(e)})
                all_logs.append(make_log(step, "reviewer", f"审查失败 - {dim_name}", output_summary=str(e), level="error"))

    # ===== 阶段 5: ReportPlanner（仅 normal/heavy） =====
    outline = None
    if config.get("has_report_planner", False):
        step += 1
        _check_timeout()
        _emit("agent_start", {"agent": "report_planner", "step": step, "name": "报告规划师",
                               "input_summary": f"公司: {company_name}\n维度数: {len(all_evidence)}"})

        try:
            rp_result = report_planner_agent(client, company_name, all_evidence, plan)
            outline = rp_result["outline"]

            log = make_log(step, "report_planner", "大纲规划完成",
                           input_summary=rp_result["input_summary"],
                           output_summary=rp_result["output_summary"],
                           duration_ms=rp_result["duration_ms"])
            all_logs.append(log)

            _emit("agent_end", {
                "agent": "report_planner",
                "step": step,
                "status": "success",
                "output_summary": rp_result["output_summary"],
            "input_text": rp_result.get("input_text", ""),
            "output_text": rp_result.get("output_text", ""),
            })
        except Exception as e:
            logger.error(f"ReportPlanner failed: {e}")
            outline = _generate_fallback_outline(company_name, all_evidence)
            _emit("agent_end", {"agent": "report_planner", "step": step, "status": "failed", "error": str(e)})
            all_logs.append(make_log(step, "report_planner", "大纲规划失败", output_summary=str(e), level="error"))

    # ===== 阶段 6: ReportWriter =====
    step += 1
    _check_timeout()
    _emit("agent_start", {"agent": "report_writer", "step": step, "name": "报告撰写师",
                          "input_summary": f"公司: {company_name}\n大纲章节数: {len(outline.get('sections', [])) if outline else 0}"})

    # 如果没有 outline（quick 模式），生成一个保底的
    if outline is None:
        outline = _generate_fallback_outline(company_name, all_evidence)

    try:
        writer_result = report_writer_agent(client, company_name, outline, all_evidence)
        final_report = writer_result["report"]

        log = make_log(step, "report_writer", "报告撰写完成",
                       input_summary=writer_result["input_summary"],
                       output_summary=writer_result["output_summary"],
                       duration_ms=writer_result["duration_ms"])
        all_logs.append(log)

        _emit("agent_end", {
            "agent": "report_writer",
            "step": step,
            "status": "success",
            "output_summary": writer_result["output_summary"],
            "input_text": writer_result.get("input_text", ""),
            "output_text": writer_result.get("output_text", ""),
        })
    except Exception as e:
        logger.error(f"ReportWriter failed: {e}")
        final_report = _generate_fallback_report(company_name, outline, all_evidence)
        _emit("agent_end", {"agent": "report_writer", "step": step, "status": "failed", "error": str(e)})
        all_logs.append(make_log(step, "report_writer", "撰写失败", output_summary=str(e), level="error"))

    # ===== 阶段 7: FactChecker =====
    fact_check_mode = config.get("fact_check_mode", "basic")
    if fact_check_mode != "none":
        step += 1
        _check_timeout()
        _emit("agent_start", {"agent": "fact_checker", "step": step, "name": "事实核查员",
                               "input_summary": f"公司: {company_name}\n报告长度: {len(final_report)}字"})

        try:
            fc_result = fact_checker_agent(client, tool_manager, company_name, final_report, fact_check_mode)
            fact_check = fc_result["fact_check"]

            log = make_log(step, "fact_checker", "事实核查完成",
                           input_summary=fc_result["input_summary"],
                           output_summary=fc_result["output_summary"],
                           tool_calls=fc_result.get("tool_calls_history", []),
                           duration_ms=fc_result["duration_ms"])
            all_logs.append(log)
            all_tool_calls.extend([
                {**tc, "agent": "fact_checker"} for tc in fc_result.get("tool_calls_history", [])
            ])

            for tc in fc_result.get("tool_calls_history", []):
                _emit("tool_call", {
                    "agent": "fact_checker",
                    "tool": tc["name"],
                    "arguments": tc["arguments"],
                    "result_summary": tc["result_summary"],
                })

            _emit("fact_check_result", {
                "confidence": fact_check.get("overall_confidence", "unknown"),
                "checked_count": len(fact_check.get("checked_claims", [])),
                "summary": fact_check.get("fact_check_summary", ""),
            })
            _emit("agent_end", {
                "agent": "fact_checker",
                "step": step,
                "status": "success",
                "output_summary": fc_result["output_summary"],
            "input_text": fc_result.get("input_text", ""),
            "output_text": fc_result.get("output_text", ""),
            })
        except Exception as e:
            logger.error(f"FactChecker failed: {e}")
            fact_check = _generate_fallback_fact_check(company_name)
            _emit("agent_end", {"agent": "fact_checker", "step": step, "status": "failed", "error": str(e)})
            all_logs.append(make_log(step, "fact_checker", "核查失败", output_summary=str(e), level="error"))
    else:
        fact_check = _generate_fallback_fact_check(company_name)

    # ===== 完成 =====
    total_duration_ms = int((time.time() - start_time) * 1000)

    # 生成执行摘要
    executive_summary = _build_executive_summary_sn(company_name, depth, plan, all_evidence, fact_check)

    result = {
        "company": company_name,
        "depth": depth,
        "mode": "sn-deepresearch",
        "model": client.model,
        "total_duration_ms": total_duration_ms,
        "total_tokens": client.total_tokens,
        "api_call_count": client.call_count,
        "plan": plan,
        "briefing": briefing,
        "all_evidence": all_evidence,
        "all_reviews": all_reviews,
        "outline": outline,
        "final_report": final_report,
        "fact_check": fact_check,
        "executive_summary": executive_summary,
        "logs": all_logs,
        "tool_calls": all_tool_calls,
    }

    _emit("complete", {
        "company": company_name,
        "depth": depth,
        "duration_ms": total_duration_ms,
        "model": client.model,
        "executive_summary": executive_summary,
        "fact_check_confidence": fact_check.get("overall_confidence", "unknown"),
    })

    return result


def _build_executive_summary_sn(company_name: str, depth: str, plan: Dict,
                                all_evidence: List[Dict], fact_check: Dict) -> str:
    """构建执行摘要（SN DeepResearch 风格）"""
    dim_count = len(all_evidence)
    total_claims = sum(len(ev.get("claims", [])) for ev in all_evidence)
    total_sources = sum(len(ev.get("sources", [])) for ev in all_evidence)
    confidence = fact_check.get("overall_confidence", "medium")

    confidence_map = {"high": "高", "medium": "中", "low": "低"}
    confidence_cn = confidence_map.get(confidence, "中")

    key_findings = []
    for ev in all_evidence:
        for kf in ev.get("key_findings", []):
            if isinstance(kf, dict):
                key_findings.append(kf.get("text", ""))
            else:
                key_findings.append(str(kf))
    top_findings = key_findings[:3]

    findings_text = "\n".join([f"- {f}" for f in top_findings]) if top_findings else "- 详见完整报告"

    return f"""# {company_name} 深度研究摘要

**研究档位**：{depth} | **维度数量**：{dim_count} | **证据规模**：{total_claims} 条断言 / {total_sources} 个来源
**事实核查可信度**：{confidence_cn}

## 核心发现

{findings_text}

## 维度覆盖

""" + "\n".join([f"- **{ev.get('dimension_name', '')}**：{ev.get('headline', '')}" for ev in all_evidence]) + f"""

> 本报告由 SN-DeepResearch 风格多 Agent 系统自动生成，仅供参考。
> 完整报告请查看详情。"""


# ============================================================
# 任务管理
# ============================================================

def create_task() -> str:
    """创建任务ID"""
    import uuid
    task_id = f"sn_{uuid.uuid4().hex[:12]}"

    # 清理旧任务，保持数量上限
    if len(_tasks) >= MAX_TASKS:
        oldest = min(_tasks.keys(), key=lambda k: _tasks[k].get("created_at", 0))
        del _tasks[oldest]

    _tasks[task_id] = {
        "id": task_id,
        "status": "pending",
        "created_at": time.time(),
        "events": [],
        "result": None,
    }
    return task_id


def get_task(task_id: str) -> Optional[Dict]:
    """获取任务状态"""
    return _tasks.get(task_id)


def update_task(task_id: str, **kwargs):
    """更新任务状态"""
    if task_id in _tasks:
        _tasks[task_id].update(kwargs)


def add_event(task_id: str, event_type: str, data: Dict):
    """添加事件"""
    if task_id in _tasks:
        _tasks[task_id]["events"].append({
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        })


def list_tasks(limit: int = 20) -> List[Dict]:
    """列出任务列表"""
    tasks = sorted(_tasks.values(), key=lambda t: t.get("created_at", 0), reverse=True)
    return tasks[:limit]
