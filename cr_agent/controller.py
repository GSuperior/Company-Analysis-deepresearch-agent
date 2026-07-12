"""Controller：编排 cr_agent 5 角色流水线。

流水线：
  scout → planner → researcher(×N) → writer(×N sections) → reviewer → render(内置)

设计要点（vs sn_agent）：
- EvidenceCard 增量追加：researcher 每搜到信息就 add_card，避免一次性写大 JSON
- 预算前置约束：Budget 在角色派发前设置，耗尽即停，而非事后检测
- Section-by-section 写作：writer 按 section 逐段派发，每次只读相关维度卡片
- Controller 内置 render：[card:dN.cM] → [N] 引用替换 + TOC + 来源列表，纯 Python 无 LLM

失败处理（借鉴 sn_agent）：
- 维度研究失败 → 降级 depth 重试 → 仍失败则跳过，不阻塞后续维度
- writer 失败 → 跳过该 section，继续后续
- reviewer verdict=fail → 记录但不重做（避免无限循环）
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from cr_agent import llm, tools

# ── 路径配置 ────────────────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _AGENT_DIR.parent
PROMPT_DIR = _AGENT_DIR / "prompts"
REPORTS_ROOT = REPO_ROOT / "reports" / "cr-agent-reports"

# 每个角色的迭代上限
ITER_BUDGET = {
    "scout": 20,
    "planner": 12,
    "researcher": 30,
    "writer": 16,
    "reviewer": 16,
}

# 每个角色的输出 token 上限
MAX_TOKENS_BUDGET = {
    "scout": 16000,
    "planner": 16000,
    "researcher": 16000,
    "writer": 16000,
    "reviewer": 16000,
}

# depth → 搜索预算映射
DEPTH_SEARCH_BUDGET = {
    "light": 4,
    "moderate": 6,
    "deep": 8,
}

# depth → 维度数量映射
DEPTH_DIM_COUNT = {
    "quick": 3,
    "normal": 5,
    "heavy": 7,
}


def _emit(event_type: str, data: dict) -> None:
    """转发到 llm 模块的全局 emit 回调。"""
    llm._emit(event_type, data)


def _log(stage: str, msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {stage} | {msg}", flush=True)
    except (BrokenPipeError, OSError):
        pass


def write_log(report_dir: Path, stage: str, status: str, extra: str = "") -> None:
    """追加一行到 pipeline.log。"""
    log_path = report_dir / "pipeline.log"
    ts = time.strftime("%H:%M:%S")
    line = f"{ts} {stage} | {status}"
    if extra:
        line += f" {extra}"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (BrokenPipeError, OSError):
        pass


# ── 报告目录 ────────────────────────────────────────────────────────────
def make_report_dir(topic: str) -> Path:
    """创建 YYYY-MM-DD-{slug}-{hex4} 报告目录骨架。"""
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    short = topic.replace("以", "").replace("作为", " ").replace("进行", " ")[:30]
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in short).strip("-")[:40]
    if not slug:
        slug = "research"
    hex4 = secrets.token_hex(2)
    d = REPORTS_ROOT / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}-{hex4}"
    for sub in ("sections",):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def load_prompt(role: str) -> str:
    return llm.load_role_prompt(str(PROMPT_DIR / f"{role}.md"))


# ── 角色派发 ────────────────────────────────────────────────────────────
def dispatch(
    role_name: str,
    system_prompt: str,
    payload: str,
    *,
    iter_key: str,
    budget: Optional[tools.Budget] = None,
    report_dir: Optional[Path] = None,
    dimension_id: Optional[str] = None,
    tools_enabled: list[str] | None = None,
) -> str:
    """派发一个角色到完成，返回其最终答复文本。

    在派发前设置工具上下文（预算、报告目录、维度 ID）。
    """
    tools.set_context(budget=budget, report_dir=report_dir, dimension_id=dimension_id)
    return llm.run_role(
        role_name=role_name,
        system_prompt=system_prompt,
        payload=payload,
        max_iterations=ITER_BUDGET.get(iter_key, 24),
        max_tokens=MAX_TOKENS_BUDGET.get(iter_key, 16000),
        tools_enabled=tools_enabled,
        verbose=True,
    )


# ── 阶段 1：scout ────────────────────────────────────────────────────────
def stage_scout(query: str, report_dir: Path) -> dict | None:
    """scout → briefing.json。返回 briefing dict 或 None。"""
    _log("scout", "派发 scout 产出领域地图")
    _emit("phase_change", {"phase": "scout", "message": "侦察阶段：搜索公司基本信息"})

    budget = tools.Budget(search_budget=5, card_budget=0)
    payload = f"""# 任务

你是 cr_agent 的 scout 角色。请阅读以下角色指令并严格执行。

## 角色指令

{load_prompt("scout")}

## 任务输入

原始需求：{query}
report_dir：{report_dir}

## 执行要求

1. 用 web_search 搜索公司基本信息（2-3 次不同关键词）
2. 可选：用 web_fetch 抓取关键页面
3. 调用 submit_briefing 提交领域地图

搜索预算：5 次。预算用尽后立即调用 submit_briefing。
"""
    dispatch(
        role_name="scout",
        system_prompt=load_prompt("scout"),
        payload=payload,
        iter_key="scout",
        budget=budget,
        report_dir=report_dir,
        tools_enabled=["web_search", "web_fetch", "submit_briefing"],
    )

    briefing_path = report_dir / "briefing.json"
    if not briefing_path.exists():
        _log("scout", "✗ briefing.json 未生成")
        write_log(report_dir, "scout", "FAIL", "briefing.json 未生成")
        return None
    try:
        briefing = json.loads(briefing_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log("scout", f"✗ briefing.json 不是合法 JSON：{e}")
        write_log(report_dir, "scout", "FAIL", f"JSON error: {e}")
        return None

    _log("scout", f"✓ briefing.json 已生成（行业={briefing.get('industry','?')}）")
    write_log(report_dir, "scout", "OK", f"industry={briefing.get('industry','?')}")
    return briefing


# ── 阶段 2：planner ──────────────────────────────────────────────────────
def stage_planner(query: str, report_dir: Path, briefing: dict, depth: str) -> dict | None:
    """planner → plan.json。返回 plan dict 或 None。"""
    _log("planner", "派发 planner 拆解维度与大纲")
    _emit("phase_change", {"phase": "planner", "message": "规划阶段：拆解研究维度与报告大纲"})

    dim_count = DEPTH_DIM_COUNT.get(depth, 5)
    briefing_json = json.dumps(briefing, ensure_ascii=False, indent=2)

    payload = f"""# 任务

你是 cr_agent 的 planner 角色。请阅读以下角色指令并严格执行。

## 角色指令

{load_prompt("planner")}

## 任务输入

原始需求：{query}
report_dir：{report_dir}
深度档位：{depth}
目标维度数量：{dim_count} 个

## briefing（来自 scout）

{briefing_json}

## 执行要求

1. 根据 briefing 设计 {dim_count} 个研究维度（id: d1, d2, ... d{dim_count}）
2. 设计报告大纲（章节数 = 维度数 + 2，含执行摘要和结论）
3. 调用 submit_plan 提交计划

不需要搜索。submit_plan 只调用一次。
"""
    dispatch(
        role_name="planner",
        system_prompt=load_prompt("planner"),
        payload=payload,
        iter_key="planner",
        budget=None,
        report_dir=report_dir,
        tools_enabled=["submit_plan"],
    )

    plan_path = report_dir / "plan.json"
    if not plan_path.exists():
        _log("planner", "✗ plan.json 未生成")
        write_log(report_dir, "planner", "FAIL", "plan.json 未生成")
        return None
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log("planner", f"✗ plan.json 不是合法 JSON：{e}")
        write_log(report_dir, "planner", "FAIL", f"JSON error: {e}")
        return None

    dims = plan.get("dimensions", [])
    outline = plan.get("outline", [])
    if not dims or not outline:
        _log("planner", "✗ plan.json 缺 dimensions 或 outline")
        write_log(report_dir, "planner", "FAIL", "缺 dimensions 或 outline")
        return None

    _log("planner", f"✓ plan.json 生成 {len(dims)} 维度 / {len(outline)} 章节")
    write_log(report_dir, "planner", "OK", f"{len(dims)} dims, {len(outline)} sections")
    return plan


# ── 阶段 3：researcher（per dimension） ──────────────────────────────────
def stage_research_one(query: str, report_dir: Path, dim: dict, depth: str) -> bool:
    """researcher 单维度 → 追加卡片到 evidence_cards.jsonl。"""
    dim_id = dim["id"]
    dim_name = dim.get("name", dim_id)
    _log("research", f"派发维度 {dim_id}/{dim_name}")
    _emit("phase_change", {
        "phase": "research",
        "message": f"研究阶段：{dim_name}",
        "dimension_id": dim_id,
    })

    search_budget = DEPTH_SEARCH_BUDGET.get(dim.get("depth", "moderate"), 6)
    budget = tools.Budget(search_budget=search_budget, card_budget=10)

    kq_lines = "\n".join(f"- {q}" for q in dim.get("key_questions", []))
    seed_lines = "\n".join(f"- {s}" for s in dim.get("search_seeds", []))

    payload = f"""# 任务

你是 cr_agent 的 researcher 角色。请阅读以下角色指令并严格执行。

## 角色指令

{load_prompt("researcher")}

## 任务输入

原始需求：{query}
report_dir：{report_dir}
dimension_id：{dim_id}

## 维度信息

名称：{dim_name}
关键问题：
{kq_lines}

种子搜索词：
{seed_lines}

depth：{dim.get("depth", "moderate")}

## 执行要求（必须严格遵守）

**核心循环：web_search → add_card → web_search → add_card → ... → submit_research**

1. 调用 web_search 搜索信息（按种子词 + 自己拓展的关键词）
2. **立即**调用 add_card 提交证据卡片（从搜索结果中提炼事实 + 复制 URL + 摘录原文）
3. 重复上述交替：每搜索一次，就提交至少 1 张卡片
4. 每个 key_question 至少有 1 张卡片覆盖
5. 搜索预算：{search_budget} 次；卡片预算：10 张
6. 所有卡片提交完毕后，调用 submit_research 声明完成

⚠ 重要提醒：
- submit_research 前必须已提交至少 1 张 add_card，否则会被拒绝
- 禁止连续搜索多次后再批量提交卡片
- 从 web_search 返回的 results 数组中提取 url 和 snippet 作为卡片来源

收敛纪律：搜索 3-4 轮后应转入卡片提交阶段。信息不足时在卡片 confidence 标 low。
"""
    dispatch(
        role_name=f"research/{dim_id}",
        system_prompt=load_prompt("researcher"),
        payload=payload,
        iter_key="researcher",
        budget=budget,
        report_dir=report_dir,
        dimension_id=dim_id,
        tools_enabled=["web_search", "web_fetch", "add_card", "submit_research"],
    )

    # 检查该维度是否产出了卡片
    cards_count = _count_cards_for_dim(report_dir, dim_id)
    if cards_count == 0:
        _log("research", f"✗ {dim_id} 未产出任何卡片")
        write_log(report_dir, f"research/{dim_id}", "FAIL", "无卡片产出")
        return False

    _log("research", f"✓ {dim_id} 产出 {cards_count} 张卡片")
    write_log(report_dir, f"research/{dim_id}", "OK", f"{cards_count} cards")
    return True


def _count_cards_for_dim(report_dir: Path, dim_id: str) -> int:
    """统计 evidence_cards.jsonl 中某维度的卡片数。"""
    cards_path = report_dir / "evidence_cards.jsonl"
    if not cards_path.exists():
        return 0
    count = 0
    with open(cards_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                card = json.loads(line)
                if card.get("dimension_id") == dim_id:
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


# ── 阶段 4：writer（per section） ─────────────────────────────────────────
def stage_write_section(
    query: str,
    report_dir: Path,
    section: dict,
    plan: dict,
    section_titles: list[str],
) -> bool:
    """writer 单章节 → sections/sN.md。"""
    section_id = section["section_id"]
    title = section.get("title", section_id)
    from_dims = section.get("from_dims", [])

    _log("writer", f"派发章节 {section_id}/{title}")
    _emit("phase_change", {
        "phase": "writer",
        "message": f"撰写章节：{title}",
        "section_id": section_id,
    })

    # 构建 from_dims 的维度信息
    dims_info = []
    for dim in plan.get("dimensions", []):
        if dim["id"] in from_dims:
            dims_info.append({
                "id": dim["id"],
                "name": dim.get("name", ""),
                "key_questions": dim.get("key_questions", []),
            })

    dims_json = json.dumps(dims_info, ensure_ascii=False, indent=2)
    all_titles = "\n".join(f"- {t}" for t in section_titles)

    is_summary = section_id == "s1"
    if is_summary:
        # 执行摘要特殊处理
        payload_extra = """\n## 特殊要求\n这是执行摘要章节。应提炼各章节核心发现，不超过 500 字。不需要标注卡片引用。\n"""
    else:
        payload_extra = ""

    payload = f"""# 任务

你是 cr_agent 的 writer 角色。请阅读以下角色指令并严格执行。

## 角色指令

{load_prompt("writer")}

## 任务输入

原始需求：{query}
report_dir：{report_dir}
section_id：{section_id}
section_title：{title}

## 关联维度

{dims_json}

## 全部章节标题（供参考）

{all_titles}
{payload_extra}
## 执行要求

1. 调用 read_cards 读取关联维度（{from_dims}）的证据卡片
2. 基于卡片内容撰写章节正文，用 [card:dN.cM] 标注引用
3. 调用 write_section 提交章节 markdown

内容长度：800-1500 字（执行摘要 300-500 字）。
"""
    dispatch(
        role_name=f"writer/{section_id}",
        system_prompt=load_prompt("writer"),
        payload=payload,
        iter_key="writer",
        budget=None,
        report_dir=report_dir,
        tools_enabled=["read_cards", "write_section"],
    )

    section_path = report_dir / "sections" / f"{section_id}.md"
    if not section_path.exists():
        _log("writer", f"✗ {section_id}.md 未生成")
        write_log(report_dir, f"writer/{section_id}", "FAIL", "未生成")
        return False

    chars = section_path.stat().st_size
    _log("writer", f"✓ {section_id}.md 已生成（{chars} 字节）")
    write_log(report_dir, f"writer/{section_id}", "OK", f"{chars} bytes")
    return True


# ── 阶段 5：reviewer ──────────────────────────────────────────────────────
def stage_reviewer(query: str, report_dir: Path, report_md: str) -> dict | None:
    """reviewer → review.json。返回 review dict 或 None。"""
    _log("reviewer", "派发 reviewer 审查终稿")
    _emit("phase_change", {"phase": "reviewer", "message": "审查阶段：质量审查终稿报告"})

    budget = tools.Budget(search_budget=2, card_budget=0)
    # 报告截断到 30KB 防止 payload 过大
    report_truncated = report_md[:30000]
    if len(report_md) > 30000:
        report_truncated += "\n\n[... 报告内容已截断，完整版见 report.md ...]"

    payload = f"""# 任务

你是 cr_agent 的 reviewer 角色。请阅读以下角色指令并严格执行。

## 角色指令

{load_prompt("reviewer")}

## 任务输入

原始需求：{query}
report_dir：{report_dir}

## 报告草稿

{report_truncated}

## 执行要求

1. 阅读完整报告草稿
2. 可选：用 read_cards 读取卡片核对引用
3. 可选：用 web_search 补查关键缺口（最多 2 次）
4. 调用 submit_review 提交审查结果

审查维度：信息完整性、事实准确性、引用合规性、整体质量。
"""
    dispatch(
        role_name="reviewer",
        system_prompt=load_prompt("reviewer"),
        payload=payload,
        iter_key="reviewer",
        budget=budget,
        report_dir=report_dir,
        tools_enabled=["read_cards", "web_search", "submit_review"],
    )

    review_path = report_dir / "review.json"
    if not review_path.exists():
        _log("reviewer", "✗ review.json 未生成（不阻塞 render）")
        write_log(report_dir, "reviewer", "WARN", "review.json 未生成")
        return None
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log("reviewer", f"✗ review.json 不是合法 JSON：{e}")
        write_log(report_dir, "reviewer", "WARN", f"JSON error: {e}")
        return None

    _log("reviewer", f"✓ review.json 已生成（verdict={review.get('verdict')}, score={review.get('overall_score')}）")
    write_log(report_dir, "reviewer", "OK", f"verdict={review.get('verdict')}, score={review.get('overall_score')}")
    return review


# ── 阶段 6：render（controller 内置，无 LLM） ─────────────────────────────
def stage_render(report_dir: Path, outline: list[dict], query: str) -> str:
    """内置 render：[card:dN.cM] → [N] 引用替换 + TOC + 来源列表。

    纯 Python 字符串处理，无 LLM 调用。
    """
    _log("render", "开始 render：引用替换 + TOC + 来源列表")
    _emit("phase_change", {"phase": "render", "message": "渲染阶段：引用替换与报告组装"})

    # 1. 读取所有卡片，构建 card_id → card 映射
    cards_map = _load_all_cards(report_dir)

    # 2. 按大纲顺序读取所有 section markdown
    sections_md = []
    citation_map: dict[str, int] = {}  # card_id → [N]
    sources_list: list[dict] = []  # 去重后的来源列表
    source_url_to_idx: dict[str, int] = {}  # url → sources_list idx

    for section in outline:
        section_id = section["section_id"]
        title = section.get("title", section_id)
        section_path = report_dir / "sections" / f"{section_id}.md"
        if not section_path.exists():
            _log("render", f"⚠ {section_id}.md 不存在，跳过")
            continue
        content = section_path.read_text(encoding="utf-8")
        sections_md.append({"section_id": section_id, "title": title, "content": content})

        # 3. 扫描 content 中的 [card:dN.cM] 引用，构建引用映射
        for match in re.finditer(r"\[card:(d\d+\.c\d+)\]", content):
            card_id = match.group(1)
            if card_id not in citation_map:
                citation_map[card_id] = len(citation_map) + 1
                # 把卡片来源加入来源列表（去重）
                card = cards_map.get(card_id)
                if card:
                    src = card.get("source", {})
                    url = src.get("url", "")
                    if url and url not in source_url_to_idx:
                        idx = len(sources_list)
                        sources_list.append({
                            "idx": idx + 1,
                            "url": url,
                            "title": src.get("title", ""),
                            "quality": src.get("quality", ""),
                        })
                        source_url_to_idx[url] = idx + 1

    # 4. 替换所有 [card:dN.cM] 为 [N]
    rendered_sections = []
    for sec in sections_md:
        content = sec["content"]
        def _replace_citation(m: re.Match) -> str:
            card_id = m.group(1)
            n = citation_map.get(card_id)
            if n is not None:
                return f"[{n}]"
            return f"[?]"  # 未找到卡片
        content = re.sub(r"\[card:(d\d+\.c\d+)\]", _replace_citation, content)
        rendered_sections.append({"section_id": sec["section_id"], "title": sec["title"], "content": content})

    # 5. 生成 TOC
    toc_lines = ["## 目录", ""]
    for i, sec in enumerate(rendered_sections, 1):
        anchor = sec["title"].lower().replace(" ", "-")
        toc_lines.append(f"{i}. [{sec['title']}](#{anchor})")
    toc_lines.append("")
    toc = "\n".join(toc_lines)

    # 6. 生成来源列表
    sources_md = ["## 来源列表", ""]
    for src in sources_list:
        title = src["title"] or src["url"]
        sources_md.append(f"{src['idx']}. [{title}]({src['url']})（{src['quality']}）")
    sources_md.append("")
    sources_section = "\n".join(sources_md)

    # 7. 组装最终报告
    header = f"# 公司深度分析报告\n\n> 本报告由 cr_agent 自动生成，基于真实联网调研。\n> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    body_parts = [header, toc]
    for i, sec in enumerate(rendered_sections, 1):
        body_parts.append(f"\n---\n\n{sec['content']}")
    body_parts.append(f"\n---\n\n{sources_section}")

    final_report = "\n".join(body_parts)

    # 8. 写入 report.md
    report_path = report_dir / "report.md"
    report_path.write_text(final_report, encoding="utf-8")

    _log("render", f"✓ report.md 已生成（{len(final_report)} 字符，{len(citation_map)} 引用，{len(sources_list)} 来源）")
    write_log(report_dir, "render", "OK", f"{len(final_report)} chars, {len(citation_map)} citations, {len(sources_list)} sources")
    return final_report


def _load_all_cards(report_dir: Path) -> dict[str, dict]:
    """读取 evidence_cards.jsonl，返回 card_id → card 映射。"""
    cards_path = report_dir / "evidence_cards.jsonl"
    if not cards_path.exists():
        return {}
    cards_map = {}
    with open(cards_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                card = json.loads(line)
                cid = card.get("card_id")
                if cid:
                    cards_map[cid] = card
            except json.JSONDecodeError:
                continue
    return cards_map


# ── 主流水线 ────────────────────────────────────────────────────────────
def run_pipeline(
    query: str,
    depth: str = "normal",
    emit: Optional[Callable[[str, dict], None]] = None,
) -> dict | None:
    """运行 cr_agent 完整流水线。

    Args:
        query: 研究需求文本
        depth: 研究深度 (quick/normal/heavy)
        emit: 事件回调

    Returns:
        结果字典，含 report_md / briefing / plan / review / report_dir 等
    """
    start_time = time.time()

    # 设置 emit 回调
    if emit:
        llm.set_emit_callback(emit)

    # 重置工具状态
    tools.reset_card_state()

    _emit("task_start", {
        "query": query,
        "depth": depth,
        "mode": "cr-agent",
        "estimated_duration": "约8-20分钟（真实联网调研）",
    })

    # 1. 创建报告目录
    report_dir = make_report_dir(query)
    _log("controller", f"报告目录：{report_dir}")
    write_log(report_dir, "controller", "START", f"depth={depth}")

    # 2. scout
    briefing = stage_scout(query, report_dir)
    if briefing is None:
        _emit("error", {"stage": "scout", "message": "scout 失败，无法继续"})
        return _build_error_result(query, depth, report_dir, "scout 阶段失败", start_time)

    # 3. planner
    plan = stage_planner(query, report_dir, briefing, depth)
    if plan is None:
        _emit("error", {"stage": "planner", "message": "planner 失败，无法继续"})
        return _build_error_result(query, depth, report_dir, "planner 阶段失败", start_time)

    dims = plan.get("dimensions", [])
    outline = plan.get("outline", [])

    # 4. researcher（per dimension，带降级重试）
    _emit("phase_change", {"phase": "research", "message": f"研究阶段：{len(dims)} 个维度并行调研"})
    successful_dims = []
    for dim in dims:
        dim_id = dim["id"]
        ok = stage_research_one(query, report_dir, dim, depth)
        if not ok:
            _emit("error", {
                "stage": "research",
                "dimension": dim_id,
                "message": f"{dim_id} 初始失败，尝试降级重试",
            })
            # 降级重试：减少搜索预算
            dim_retry = dict(dim)
            dim_retry["depth"] = "light"
            ok = stage_research_one(query, report_dir, dim_retry, depth)
            if not ok:
                _log("controller", f"⚠ {dim_id} 降级重试仍失败，跳过该维度")
                write_log(report_dir, f"research/{dim_id}", "FAIL-skip", "降级重试仍失败")
                continue
        successful_dims.append(dim_id)

    if not successful_dims:
        _emit("error", {"stage": "research", "message": "所有维度研究均失败"})
        return _build_error_result(query, depth, report_dir, "所有维度研究均失败", start_time)

    _log("controller", f"研究完成：{len(successful_dims)}/{len(dims)} 维度成功")

    # 5. writer（per section）
    section_titles = [s.get("title", s["section_id"]) for s in outline]
    successful_sections = []
    for section in outline:
        ok = stage_write_section(query, report_dir, section, plan, section_titles)
        if ok:
            successful_sections.append(section["section_id"])
        else:
            _log("controller", f"⚠ 章节 {section['section_id']} 写作失败，跳过")

    if not successful_sections:
        _emit("error", {"stage": "writer", "message": "所有章节写作均失败"})
        return _build_error_result(query, depth, report_dir, "所有章节写作均失败", start_time)

    # 6. render（内置）
    # 先拼接所有 section 供 reviewer 审查
    draft_md = _assemble_draft(report_dir, outline)
    draft_path = report_dir / "draft.md"
    draft_path.write_text(draft_md, encoding="utf-8")

    # 7. reviewer
    review = stage_reviewer(query, report_dir, draft_md)

    # 8. render（引用替换 + TOC + 来源列表）
    report_md = stage_render(report_dir, outline, query)

    elapsed = time.time() - start_time
    _log("controller", f"✓ 流水线完成（耗时 {elapsed:.0f}s）")
    write_log(report_dir, "controller", "DONE", f"elapsed={elapsed:.0f}s")

    _emit("complete", {
        "report": report_md,
        "report_length": len(report_md),
        "total_duration_ms": int(elapsed * 1000),
    })

    return {
        "report_md": report_md,
        "report_md_path": str(report_dir / "report.md"),
        "report_dir": str(report_dir),
        "briefing": briefing,
        "plan": plan,
        "review": review or {},
        "dim_ids": successful_dims,
        "section_ids": successful_sections,
        "pipeline_log": str(report_dir / "pipeline.log"),
        "elapsed_seconds": elapsed,
    }


def _assemble_draft(report_dir: Path, outline: list[dict]) -> str:
    """拼接所有 section 为 draft.md（供 reviewer 审查）。"""
    parts = ["# 公司深度分析报告（草稿）\n"]
    for section in outline:
        section_id = section["section_id"]
        title = section.get("title", section_id)
        section_path = report_dir / "sections" / f"{section_id}.md"
        if section_path.exists():
            content = section_path.read_text(encoding="utf-8")
            parts.append(content)
        else:
            parts.append(f"## {title}\n\n[章节未生成]\n")
    return "\n\n".join(parts)


def _build_error_result(query: str, depth: str, report_dir: Path, error: str, start_time: float) -> dict:
    """构建错误结果。"""
    elapsed = time.time() - start_time
    return {
        "error": error,
        "report_md": "",
        "report_dir": str(report_dir),
        "pipeline_log": str(report_dir / "pipeline.log"),
        "elapsed_seconds": elapsed,
    }
