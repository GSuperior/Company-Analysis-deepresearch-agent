"""cr_agent 工具层：复用 sn_agent/tools.py 的基础工具 + 新增业务工具。

业务工具：
- add_card: 追加一张证据卡片到 evidence_cards.jsonl
- write_section: 写入一个章节的 markdown
- submit_briefing / submit_plan / submit_research / submit_review: 提交结构化结果
- read_cards: 读取指定维度的卡片

预算计数：
- 每个角色运行时创建 Budget 实例，通过 set_budget() 设置
- web_search / web_fetch 调用计数，耗尽后返回 budget_exhausted
"""
from __future__ import annotations

import json
import os
import re
import hashlib
from pathlib import Path
from typing import Any, Optional

# 复用 sn_agent 的基础工具
from sn_agent.tools import (
    web_search as _base_web_search,
    web_fetch as _base_web_fetch,
    read_file as _base_read_file,
    write_file as _base_write_file,
    list_files as _base_list_files,
    run_command as _base_run_command,
    TOOL_SPECS as _BASE_SPECS,
)


# ── 预算管理 ──────────────────────────────────────────────────────────
class Budget:
    """角色运行时的工具调用预算。"""
    def __init__(self, search_budget: int = 6, card_budget: int = 8):
        self.search_budget = search_budget
        self.card_budget = card_budget
        self.search_used = 0
        self.card_used = 0

    @property
    def search_remaining(self) -> int:
        return max(0, self.search_budget - self.search_used)

    @property
    def card_remaining(self) -> int:
        return max(0, self.card_budget - self.card_used)

    def consume_search(self) -> bool:
        if self.search_used >= self.search_budget:
            return False
        self.search_used += 1
        return True

    def consume_card(self) -> bool:
        if self.card_used >= self.card_budget:
            return False
        self.card_used += 1
        return True


# 当前角色预算（由 controller 设置）
_current_budget: Optional[Budget] = None
# 当前报告目录
_current_report_dir: Optional[Path] = None
# 当前维度 ID（researcher 运行时设置）
_current_dimension_id: Optional[str] = None
# 卡片计数器（per dimension）
_card_counter: dict[str, int] = {}
# 已提交的卡片文本 hash（去重）
_seen_card_hashes: set[str] = set()
# submit 结果存储
_submit_results: dict[str, Any] = {}


def set_context(budget: Optional[Budget] = None, report_dir: Optional[Path] = None,
                dimension_id: Optional[str] = None) -> None:
    """设置当前工具上下文（每次角色派发前调用）。"""
    global _current_budget, _current_report_dir, _current_dimension_id
    _current_budget = budget
    _current_report_dir = report_dir
    _current_dimension_id = dimension_id


def reset_card_state() -> None:
    """重置卡片计数器和去重集合（新任务时调用）。"""
    global _card_counter, _seen_card_hashes
    _card_counter = {}
    _seen_card_hashes = set()


def get_submit_result(key: str) -> Any:
    return _submit_results.get(key)


# ── 工具实现 ──────────────────────────────────────────────────────────

def _do_web_search(args: dict) -> str:
    if _current_budget and not _current_budget.consume_search():
        return json.dumps({
            "budget_exhausted": True,
            "message": f"搜索预算已用尽（{_current_budget.search_budget}次），请立即提交卡片或调用 submit_research",
        }, ensure_ascii=False)
    query = args.get("query", "")
    num = args.get("num", 5)
    result = _base_web_search(query, num)
    # 在结果中追加预算提示
    if _current_budget and _current_budget.search_remaining <= 1:
        try:
            result_data = json.loads(result)
            if isinstance(result_data, list):
                result_data = {"results": result_data, "budget_warning": f"搜索预算仅剩 {_current_budget.search_remaining} 次"}
            elif isinstance(result_data, dict):
                result_data["budget_warning"] = f"搜索预算仅剩 {_current_budget.search_remaining} 次"
            result = json.dumps(result_data, ensure_ascii=False)
        except Exception:
            pass
    return result


def _do_web_fetch(args: dict) -> str:
    if _current_budget and not _current_budget.consume_search():
        return json.dumps({
            "budget_exhausted": True,
            "message": "抓取预算已用尽，请立即提交卡片或调用 submit_research",
        }, ensure_ascii=False)
    url = args.get("url", "")
    return _base_web_fetch(url)


def _do_add_card(args: dict) -> str:
    """追加一张证据卡片到 evidence_cards.jsonl。"""
    # 校验必填字段
    required = ["dimension_id", "key_question_id", "text", "source_url", "snippet"]
    for field in required:
        if not args.get(field):
            return json.dumps({"error": f"missing_field", "field": field}, ensure_ascii=False)

    dim_id = args["dimension_id"]
    text = args["text"]

    # 消费卡片预算
    if _current_budget and not _current_budget.consume_card():
        return json.dumps({
            "budget_exhausted": True,
            "message": f"卡片预算已用尽（{_current_budget.card_budget}张），请调用 submit_research",
        }, ensure_ascii=False)

    # 去重检查
    normalized = re.sub(r"\s+", "", text.lower())[:200]
    card_hash = hashlib.md5(normalized.encode()).hexdigest()
    if card_hash in _seen_card_hashes:
        return json.dumps({
            "duplicate": True,
            "message": "该信息已提交过相似卡片，请换角度搜索或跳过此信息点",
        }, ensure_ascii=False)
    _seen_card_hashes.add(card_hash)

    # 分配 card_id
    _card_counter[dim_id] = _card_counter.get(dim_id, 0) + 1
    card_seq = _card_counter[dim_id]
    card_id = f"{dim_id}.c{card_seq}"

    # 构建卡片
    card = {
        "card_id": card_id,
        "dimension_id": dim_id,
        "key_question_id": args["key_question_id"],
        "text": text,
        "kind": args.get("kind", "factual"),
        "source": {
            "url": args["source_url"],
            "title": args.get("source_title", ""),
            "quality": args.get("source_quality", "secondary"),
            "snippet": args["snippet"],
        },
        "confidence": args.get("confidence", "medium"),
    }

    # 落盘
    if _current_report_dir:
        cards_path = _current_report_dir / "evidence_cards.jsonl"
        with open(cards_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")

    card_remaining = _current_budget.card_remaining if _current_budget else "unlimited"
    return json.dumps({
        "ok": True,
        "card_id": card_id,
        "cards_in_dim": _card_counter[dim_id],
        "budget_remaining": card_remaining,
    }, ensure_ascii=False)


def _do_write_section(args: dict) -> str:
    """写入一个章节的 markdown。"""
    section_id = args.get("section_id", "")
    title = args.get("title", "")
    content = args.get("content", "")

    if not section_id or not content:
        return json.dumps({"error": "missing section_id or content"}, ensure_ascii=False)

    if _current_report_dir:
        sections_dir = _current_report_dir / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)
        path = sections_dir / f"{section_id}.md"
        # 写入标题 + 内容
        full_content = f"## {title}\n\n{content}\n"
        path.write_text(full_content, encoding="utf-8")

    return json.dumps({
        "ok": True,
        "section_id": section_id,
        "chars": len(content),
    }, ensure_ascii=False)


def _do_read_cards(args: dict) -> str:
    """读取指定维度的卡片。"""
    dimension_ids = args.get("dimension_ids", [])
    if not dimension_ids:
        return json.dumps({"error": "missing dimension_ids"}, ensure_ascii=False)

    if not _current_report_dir:
        return json.dumps({"error": "no report_dir"}, ensure_ascii=False)

    cards_path = _current_report_dir / "evidence_cards.jsonl"
    if not cards_path.exists():
        return json.dumps({"cards": [], "message": "no cards yet"}, ensure_ascii=False)

    dim_set = set(dimension_ids)
    cards = []
    with open(cards_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                card = json.loads(line)
                if card.get("dimension_id") in dim_set:
                    cards.append(card)
            except json.JSONDecodeError:
                continue

    return json.dumps({"cards": cards, "count": len(cards)}, ensure_ascii=False)


def _do_submit_briefing(args: dict) -> str:
    """提交 briefing 并存储。"""
    _submit_results["briefing"] = args
    if _current_report_dir:
        path = _current_report_dir / "briefing.json"
        path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"ok": True, "message": "briefing 已提交"}, ensure_ascii=False)


def _do_submit_plan(args: dict) -> str:
    """提交 plan 并存储。"""
    _submit_results["plan"] = args
    if _current_report_dir:
        path = _current_report_dir / "plan.json"
        path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"ok": True, "message": "plan 已提交"}, ensure_ascii=False)


def _do_submit_research(args: dict) -> str:
    """提交研究完成声明。会校验该维度是否已提交足够卡片。"""
    dim_id = args.get("dimension_id", _current_dimension_id or "unknown")
    # 校验：该维度必须已提交至少 1 张卡片
    cards_count = _card_counter.get(dim_id, 0)
    if cards_count == 0:
        return json.dumps({
            "error": "no_cards_submitted",
            "message": f"维度 {dim_id} 尚未提交任何证据卡片（add_card）。",
            "hint": "请先调用 add_card 提交搜索到的信息，每条搜索结果至少提交 1 张卡片，"
                    "然后再调用 submit_research。流程：web_search → add_card → submit_research。",
            "dimension_id": dim_id,
        }, ensure_ascii=False)
    _submit_results[f"research_{dim_id}"] = args
    return json.dumps({
        "ok": True,
        "dimension_id": dim_id,
        "cards_submitted": cards_count,
    }, ensure_ascii=False)


def _do_submit_review(args: dict) -> str:
    """提交审查结果。"""
    _submit_results["review"] = args
    if _current_report_dir:
        path = _current_report_dir / "review.json"
        path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"ok": True, "message": "review 已提交"}, ensure_ascii=False)


# ── 工具规格（function-calling 定义） ─────────────────────────────────

_BASE_TOOL_NAMES = {"web_search", "web_fetch", "read_file", "write_file", "list_files", "run_command"}

CR_TOOL_SPECS = list(_BASE_SPECS)  # 复制基础工具规格

# 新增业务工具规格
_BUSINESS_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "add_card",
            "description": "提交一张证据卡片。每得到一条可信信息就调用一次。必须附带来源 URL 和原文摘录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension_id": {"type": "string", "description": "所属维度ID，如 d1"},
                    "key_question_id": {"type": "string", "description": "回答的关键问题ID，如 kq1"},
                    "text": {"type": "string", "description": "卡片内容：一条具体的事实/判断，200字以内"},
                    "kind": {"type": "string", "enum": ["factual", "interpretive", "contextual"], "description": "卡片类型"},
                    "source_url": {"type": "string", "description": "信息来源 URL，必须可访问"},
                    "source_title": {"type": "string", "description": "来源页面标题"},
                    "source_quality": {"type": "string", "enum": ["primary", "secondary", "tertiary"], "description": "来源质量"},
                    "snippet": {"type": "string", "description": "原文摘录，用于核查"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"], "description": "置信度"},
                },
                "required": ["dimension_id", "key_question_id", "text", "source_url", "snippet"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_section",
            "description": "写入一个章节的 markdown 内容。引用卡片用 [card:dN.cM] 标记。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "string", "description": "章节ID，如 s2"},
                    "title": {"type": "string", "description": "章节标题"},
                    "content": {"type": "string", "description": "章节 markdown 正文，800-1500字，引用用 [card:dN.cM]"},
                },
                "required": ["section_id", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_cards",
            "description": "读取指定维度的证据卡片。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "维度ID列表，如 [\"d1\", \"d2\"]",
                    },
                },
                "required": ["dimension_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_briefing",
            "description": "提交领域地图（briefing）。只能调用一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_summary": {"type": "string", "description": "一句话公司定位"},
                    "industry": {"type": "string", "description": "所属行业"},
                    "listed": {"type": "string", "description": "上市状态+股票代码"},
                    "known_dimensions": {"type": "array", "items": {"type": "string"}, "description": "已知维度"},
                    "key_entities": {"type": "array", "items": {"type": "string"}, "description": "关键实体"},
                    "recommended_depth": {"type": "string", "description": "建议深度"},
                    "search_tips": {"type": "array", "items": {"type": "string"}, "description": "搜索建议"},
                },
                "required": ["company_summary", "known_dimensions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "提交研究计划（维度+大纲）。只能调用一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "key_questions": {"type": "array", "items": {"type": "string"}},
                                "search_seeds": {"type": "array", "items": {"type": "string"}},
                                "depth": {"type": "string"},
                            },
                        },
                    },
                    "outline": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section_id": {"type": "string"},
                                "title": {"type": "string"},
                                "from_dims": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
                "required": ["dimensions", "outline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_research",
            "description": "声明该维度研究完成。只能调用一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension_id": {"type": "string", "description": "维度ID"},
                    "summary": {"type": "string", "description": "该维度研究总结"},
                },
                "required": ["dimension_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "提交审查结果。只能调用一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["pass", "revise", "fail"]},
                    "overall_score": {"type": "number"},
                    "gaps": {"type": "array", "items": {"type": "object"}},
                    "conflicts": {"type": "array", "items": {"type": "object"}},
                    "unverified_claims": {"type": "array", "items": {"type": "string"}},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["verdict", "overall_score"],
            },
        },
    },
]

CR_TOOL_SPECS.extend(_BUSINESS_SPECS)

def _do_read_file(args: dict) -> str:
    return _base_read_file(args.get("path", ""))

def _do_write_file(args: dict) -> str:
    return _base_write_file(args.get("path", ""), args.get("content", ""))

def _do_list_files(args: dict) -> str:
    return _base_list_files(args.get("path", ""))

def _do_run_command(args: dict) -> str:
    return _base_run_command(args.get("command", ""), args.get("cwd"))


# 工具名 → 实现函数映射
_TOOL_IMPLEMENTATIONS = {
    "web_search": _do_web_search,
    "web_fetch": _do_web_fetch,
    "read_file": _do_read_file,
    "write_file": _do_write_file,
    "list_files": _do_list_files,
    "run_command": _do_run_command,
    "add_card": _do_add_card,
    "write_section": _do_write_section,
    "read_cards": _do_read_cards,
    "submit_briefing": _do_submit_briefing,
    "submit_plan": _do_submit_plan,
    "submit_research": _do_submit_research,
    "submit_review": _do_submit_review,
}


def execute_tool(name: str, args: dict) -> str:
    """执行工具调用，返回结果字符串。"""
    impl = _TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return json.dumps({"error": f"unknown_tool: {name}"}, ensure_ascii=False)
    try:
        return impl(args)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def get_tool_specs(enabled: list[str] | None = None) -> list[dict]:
    """获取工具规格。enabled=None 返回全部，否则只返回指定工具。"""
    if enabled is None:
        return CR_TOOL_SPECS
    names = set(enabled)
    return [t for t in CR_TOOL_SPECS if t["function"]["name"] in names]
