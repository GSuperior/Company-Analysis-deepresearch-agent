"""Controller：编排 sn-deep-research normal 模式流水线。

按 SKILL.md §4.2 normal 流水线实现：
  scout → plan → research(×N) → evidence validator → review(子报告)
        → report-planner → outline validator → report-writer(full_outline)
        → review(终稿) → render

每个角色通过 llm.run_role 执行；controller 只做：
- 建报告目录、读小调度字段、跑 validator、派下一角色、按 §4.3 处理失败重试。
- 大文件（evidence / outline / sections）一律通过绝对路径传给角色自读。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from sn_agent import llm

# ── 路径配置 ────────────────────────────────────────────────────────────
# sn_agent/skill/ 下放了 sn-deep-research 全套 spec + scripts + schemas
_AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _AGENT_DIR.parent
SKILL_DIR = _AGENT_DIR / "skill"
PLUGIN_SKILLS_DIR = SKILL_DIR
ROLE_DIR = SKILL_DIR / "agents"
VALIDATE_EVIDENCE = SKILL_DIR / "scripts" / "validate_evidence.py"
VALIDATE_OUTLINE = SKILL_DIR / "scripts" / "validate_outline.py"
PREPARE_CITATIONS = SKILL_DIR / "sn-prepare-citations" / "scripts" / "prepare_citations.py"
REPORTS_ROOT = REPO_ROOT / "reports" / "deep-research-reports"


def _emit(event_type: str, data: dict) -> None:
    """转发到 llm 模块的全局 emit 回调（统一出口）。"""
    llm._emit(event_type, data)


def _log(stage: str, msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {stage} | {msg}", flush=True)
    except (BrokenPipeError, OSError):
        pass

# 每个角色的迭代上限——按 §5 各角色工作量估算
ITER_BUDGET = {
    "scout": 22,
    "plan": 14,
    "research": 40,
    "review_sub": 16,
    "report_planner": 24,
    "report_writer": 32,
    "review_final": 18,
}

# 每个角色的输出 token 上限——report-planner 需要更大的预算，因为
# outline.json（含 sections[].blocks[].thesis 等大段中文）可达 30KB+，
# 默认 16000 + thinking 模式会导致输出被截断。
MAX_TOKENS_BUDGET = {
    "scout": 16000,
    "plan": 16000,
    "research": 16000,
    "review_sub": 16000,
    "report_planner": 32000,
    "report_writer": 32000,
    "review_final": 16000,
}


def _log(stage: str, msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {stage} | {msg}", flush=True)
    except (BrokenPipeError, OSError):
        pass


# ── 报告目录 ────────────────────────────────────────────────────────────
def make_report_dir(topic: str) -> Path:
    """按 §3 创建 YYYY-MM-DD-{topic}-{hex4} 报告目录骨架。"""
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    # topic 可能是中文长句，只取前几个词做 slug
    short = topic.replace("以", "").replace("作为", " ").replace("进行", " ")[:30]
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in short).strip("-")[:40]
    if not slug:
        slug = "research"
    hex4 = secrets.token_hex(2)
    d = REPORTS_ROOT / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}-{hex4}"
    for sub in ("sub_reports", "board", "sections"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


# ── validator / render 包装 ─────────────────────────────────────────────
def run_validator(script: Path, *args: str, cwd: Path | None = None) -> dict:
    """跑 validator 脚本，解析 stdout JSON。返回 {"ok": bool, ...}。"""
    cmd = ["python3", str(script), *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": [{"rule": "TIMEOUT", "message": "validator 超时"}]}
    out = proc.stdout.strip()
    try:
        return json.loads(out) if out else {"ok": False, "errors": [{"rule": "NO_OUTPUT"}]}
    except json.JSONDecodeError:
        return {
            "ok": False,
            "errors": [{"rule": "BAD_JSON", "message": out[:500]}],
            "stderr": proc.stderr[:500],
        }


def validate_evidence(ev_path: Path) -> dict:
    return run_validator(VALIDATE_EVIDENCE, ev_path)


def validate_outline(outline_path: Path, subsets_dir: Path, evidence_paths: list[Path]) -> dict:
    args = [outline_path, "--subsets", subsets_dir, "--evidence", *evidence_paths]
    return run_validator(VALIDATE_OUTLINE, *args)


def render_report(report_md_in: Path, outline: Path | None, evidence_paths: list[Path], output: Path) -> dict:
    args = ["--report", report_md_in, "--evidence", *evidence_paths, "--output", output]
    if outline is not None:
        args += ["--outline", outline]
    return run_validator(PREPARE_CITATIONS, *args)


# ── 角色派发 ────────────────────────────────────────────────────────────
def dispatch(role_name: str, system_prompt: str, payload: str, *, iter_key: str,
             tools_enabled: list[str] | None = None) -> str:
    """派发一个角色到完成，返回其最终答复文本。"""
    return llm.run_role(
        role_name=role_name,
        system_prompt=system_prompt,
        payload=payload,
        max_iterations=ITER_BUDGET[iter_key],
        max_tokens=MAX_TOKENS_BUDGET.get(iter_key, 16000),
        tools_enabled=tools_enabled,
        verbose=True,
    )


def load_prompt(role: str) -> str:
    return llm.load_role_prompt(str(ROLE_DIR / f"{role}.md"))


# ── 阶段实现 ────────────────────────────────────────────────────────────
def stage_scout(query: str, report_dir: Path) -> bool:
    """§5.1 scout → briefing.json。"""
    _log("scout", "派发 scout 产出领域地图")
    payload = f"""先读取 {ROLE_DIR}/scout.md 并严格遵守。

原始需求:{query}
report_dir:{report_dir}

请按 scout agent 契约产出 briefing，并写入：
{report_dir}/briefing.json
"""
    dispatch("scout", load_prompt("scout"), payload, iter_key="scout")
    briefing = report_dir / "briefing.json"
    if not briefing.exists():
        _log("scout", "✗ briefing.json 未生成")
        return False
    # 调度字段存在性检查
    try:
        b = json.loads(briefing.read_text(encoding="utf-8"))
        if not b.get("recommended_mode") or not b.get("mode_rationale"):
            _log("scout", "✗ briefing 缺 recommended_mode/mode_rationale")
            return False
    except json.JSONDecodeError as e:
        _log("scout", f"✗ briefing.json 不是合法 JSON：{e}")
        return False
    _log("scout", f"✓ briefing.json 已生成（mode={b.get('recommended_mode')}）")
    return True


def stage_plan(query: str, report_dir: Path, mode: str) -> dict | None:
    """§5.2 plan → blueprint.json + plan.json。返回 plan dict。"""
    _log("plan", f"派发 plan（mode={mode}）")
    payload = f"""先读取 {ROLE_DIR}/plan.md 并严格遵守。

原始需求:{query}
report_dir:{report_dir}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}
briefing_path:{report_dir}/briefing.json
mode:{mode}
user_clarification_answers:{{}}

请按 plan agent 契约完成报告格式判定、研究维度拆解、wave/depends_on 规划与 lenses 规划。
输出：
- {report_dir}/blueprint.json
- {report_dir}/plan.json
"""
    dispatch("plan", load_prompt("plan"), payload, iter_key="plan")
    plan_path = report_dir / "plan.json"
    if not plan_path.exists():
        _log("plan", "✗ plan.json 未生成")
        return None
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log("plan", f"✗ plan.json 不是合法 JSON：{e}")
        return None
    dims = plan.get("dimensions", [])
    if not dims:
        _log("plan", "✗ plan.json 缺 dimensions")
        return None
    _log("plan", f"✓ plan.json 生成 {len(dims)} 个维度：{[d['id']+'/'+d['name'] for d in dims]}")
    return plan


def stage_research_one(query: str, report_dir: Path, dim: dict, *, validator_errors: list | None = None) -> bool:
    """§5.3 research → sub_reports/d{N}.evidence.json。

    validator_errors 非空时为修复模式：把 validator 错误清单注入 payload，
    并指向已存在的 evidence.json，让 agent 增量修复而非重做。
    """
    dim_id = dim["id"]
    if validator_errors:
        _log("research", f"派发维度 {dim_id} 修复（{len(validator_errors)} 个 validator 错误）")
    else:
        _log("research", f"派发维度 {dim_id}/{dim['name']}")
    kq_lines = "\n".join(f"- kq{i+1}: {q}" for i, q in enumerate(dim.get("key_questions", [])))
    sources = dim.get("sources", [])
    src_lines = json.dumps(sources, ensure_ascii=False) if sources else "[]"

    # 修复模式 vs 初始模式
    if validator_errors:
        existing_ev = report_dir / "sub_reports" / f"{dim_id}.evidence.json"
        err_block = json.dumps(validator_errors, ensure_ascii=False, indent=2)
        mode_line = "initial  # 实为修复重跑：本环境未走 supplement 流程；按 validator 错误增量修复现有 evidence"
        fix_directive = f"""
修复指令（必读）：
现有 evidence 已生成但 validator 报错，请按下列错误清单增量修复（不要重做）：
- 读取现有 {existing_ev}
- 针对每个错误定位字段（按 rule + 错误位置），就地修正
- 常见修复模式：
  * V011 source.id 不合法：source.id 必须形如 ^[a-z][a-z0-9_]*$（小写字母开头，仅小写字母/数字/下划线），不能以数字开头
  * V012 重复 source.id：合并为同一 source 或重命名
  * V040 factual 缺 primary/secondary：补一手来源 evidence（财报/公告/官网）
  * V041 interpretive 缺第二来源：补一个不同 source 的独立 evidence
  * V053 key_findings.claim_ids 引用不存在的 claim：删除该 claim_id 或新建对应 claim
  * 其他 V### 错误：按 message 定位字段修正
- 不要重新搜索大量新来源；只针对 validator 报错的 claim/source 做最小修复
- 修复后必须重新跑 validator

validator 错误清单：
{err_block}
"""
    else:
        mode_line = "initial"
        fix_directive = ""

    payload = f"""先读取 {ROLE_DIR}/research.md 并严格遵守。

原始需求:{query}
mode:{mode_line}

report_dir:{report_dir}
dimension_id:{dim_id}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}

name:{dim.get('name','')}
description:{dim.get('description','')}
key_questions:
{kq_lines}
focus:{dim.get('focus','')}
context_from_briefing:{dim.get('context_from_briefing','')}
sources:{src_lines}
depth:{dim.get('depth','moderate')}
time_sensitivity:{dim.get('time_sensitivity','')}
upstream_evidence:
-  # 无依赖（normal 单 wave）

来源纪律:搜索入口按 sources category 选择对应相关的 skill；source.url 写原始 URL。
本环境仅提供通用网页搜索与抓取（无 sn-search-finance 等专业 skill 与 cookie），sources 命中专业类别时按 research.md「能力降级契约」用通用搜索兜底，不返回 blocked。

收敛纪律（重要）：搜索 5-8 轮后应立即转入写 evidence.json 阶段。
- 不要为追求完美来源而反复搜索同一关键词（系统会检测重复查询并强制收敛）
- 某个 key_question 信息不足时，在 evidence 中如实标注信息缺口即可
- evidence.json 的按时产出比搜索覆盖度更重要——先写出来再修复

schema_path:{SKILL_DIR}/schemas/evidence.schema.md
output_path:{report_dir}/sub_reports/{dim_id}.evidence.json
{fix_directive}
"""
    dispatch(
        "research",
        load_prompt("research"),
        payload,
        iter_key="research",
        tools_enabled=["web_search", "web_fetch", "read_file", "write_file", "list_files", "run_command"],
    )
    ev_path = report_dir / "sub_reports" / f"{dim_id}.evidence.json"
    if not ev_path.exists():
        _log("research", f"✗ {dim_id}.evidence.json 未生成")
        return False
    _log("research", f"✓ {dim_id}.evidence.json 已生成")
    return True


def stage_evidence_validator(report_dir: Path, dim_id: str) -> bool:
    """§5.4 controller 跑 evidence validator。"""
    ev_path = report_dir / "sub_reports" / f"{dim_id}.evidence.json"
    _log("ev-validator", f"校验 {dim_id}.evidence.json")
    res = validate_evidence(ev_path)
    if res.get("ok"):
        stats = res.get("stats", {})
        _log("ev-validator", f"✓ {dim_id} 通过：{stats}")
        return True
    _log("ev-validator", f"✗ {dim_id} 失败：{json.dumps(res.get('errors', [])[:3], ensure_ascii=False)}")
    return False


def stage_review_sub(query: str, report_dir: Path, dim: dict) -> bool:
    """§5.5 review（子报告）→ sub_reports/d{N}.review.md。"""
    dim_id = dim["id"]
    _log("review-sub", f"派发 {dim_id} 子报告审查")
    kq_lines = "\n".join(f"- kq{i+1}: {q}" for i, q in enumerate(dim.get("key_questions", [])))
    payload = f"""先读取 {ROLE_DIR}/review.md 并严格遵守。

原始需求:{query}
审查类型:子报告 evidence 审查

report_dir:{report_dir}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}
dimension_id:{dim_id}
evidence_path:{report_dir}/sub_reports/{dim_id}.evidence.json
output_path:{report_dir}/sub_reports/{dim_id}.review.md

key_questions:
{kq_lines}
depth:{dim.get('depth','moderate')}
time_sensitivity:{dim.get('time_sensitivity','')}
"""
    dispatch(
        f"review/{dim_id}",
        load_prompt("review"),
        payload,
        iter_key="review_sub",
        tools_enabled=["web_search", "web_fetch", "read_file", "write_file", "list_files"],
    )
    out = report_dir / "sub_reports" / f"{dim_id}.review.md"
    if not out.exists():
        _log("review-sub", f"⚠ {dim_id}.review.md 未生成（不阻塞，继续）")
        return False
    _log("review-sub", f"✓ {dim_id}.review.md 已生成")
    return True


def stage_report_planner(query: str, report_dir: Path, dim_ids: list[str]) -> bool:
    """§5.8 report-planner → outline.json + sections/s*.evidence_subset.json。"""
    _log("report-planner", "派发 report-planner")
    ev_paths = "\n".join(
        f"- {report_dir}/sub_reports/{d}.evidence.json" for d in dim_ids
    )
    payload = f"""先读取 {ROLE_DIR}/report-planner.md 并严格遵守。

原始需求:{query}

report_dir:{report_dir}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}
briefing_path:{report_dir}/briefing.json
blueprint_path:{report_dir}/blueprint.json
plan_path:{report_dir}/plan.json
evidence_paths:
{ev_paths}
schema_path:{SKILL_DIR}/schemas/outline.schema.md

output_outline:{report_dir}/outline.json
output_subsets_dir:{report_dir}/sections/
"""
    dispatch(
        "report-planner",
        load_prompt("report-planner"),
        payload,
        iter_key="report_planner",
        tools_enabled=["read_file", "write_file", "list_files", "run_command"],
    )
    if not (report_dir / "outline.json").exists():
        _log("report-planner", "✗ outline.json 未生成")
        return False
    _log("report-planner", "✓ outline.json 已生成")
    return True


def stage_outline_validator(report_dir: Path, dim_ids: list[str]) -> list[str] | None:
    """§5.9 controller 跑 outline validator。返回 section_ids（成功）或 None。"""
    _log("outline-validator", "校验 outline + subsets")
    outline = report_dir / "outline.json"
    subsets_dir = report_dir / "sections"
    ev_paths = [report_dir / "sub_reports" / f"{d}.evidence.json" for d in dim_ids]
    res = validate_outline(outline, subsets_dir, ev_paths)
    if res.get("ok"):
        sections = []
        try:
            o = json.loads(outline.read_text(encoding="utf-8"))
            sections = [s["id"] for s in o.get("sections", [])]
        except Exception:
            pass
        _log("outline-validator", f"✓ outline 通过；sections={sections}")
        return sections
    _log("outline-validator", f"✗ outline 失败：{json.dumps(res.get('errors', [])[:3], ensure_ascii=False)}")
    return None


def stage_report_writer(query: str, report_dir: Path) -> bool:
    """§5.10 report-writer(full_outline) → sections/s_full.md。"""
    _log("report-writer", "派发 report-writer（full_outline）")
    payload = f"""先读取 {ROLE_DIR}/report-writer.md 并严格遵守。

原始需求:{query}

report_dir:{report_dir}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}
section_id:s_full
write_mode:full_outline

outline_path:{report_dir}/outline.json
evidence_subset_path:  # full_outline 模式：writer 自读 sections/s*.evidence_subset.json 全部切片
output_path:{report_dir}/sections/s_full.md
"""
    dispatch(
        "report-writer",
        load_prompt("report-writer"),
        payload,
        iter_key="report_writer",
        tools_enabled=["read_file", "write_file", "list_files"],
    )
    out = report_dir / "sections" / "s_full.md"
    if not out.exists():
        _log("report-writer", "✗ s_full.md 未生成")
        return False
    size = out.stat().st_size
    _log("report-writer", f"✓ s_full.md 已生成（{size} 字节）")
    return True


def stage_review_final(query: str, report_dir: Path, dim_ids: list[str]) -> bool:
    """§5.5 终稿 review（normal 版）→ final_review.md。"""
    _log("review-final", "派发终稿 review")
    ev_paths = "\n".join(f"- {report_dir}/sub_reports/{d}.evidence.json" for d in dim_ids)
    rv_paths = "\n".join(f"- {report_dir}/sub_reports/{d}.review.md" for d in dim_ids)
    payload = f"""先读取 {ROLE_DIR}/review.md 并严格遵守。

原始需求:{query}
审查类型:终稿 review

report_dir:{report_dir}
plugin_skills_dir:{PLUGIN_SKILLS_DIR}
stitched_path:{report_dir}/sections/s_full.md
outline_path:{report_dir}/outline.json
evidence_paths:
{ev_paths}
review_paths:
{rv_paths}

请按 review agent 的终稿审查契约检查整体逻辑、引用纪律、冲突/gap surface 与 evidence 边界。
把审查结论写入：{report_dir}/final_review.md
"""
    dispatch(
        "review-final",
        load_prompt("review"),
        payload,
        iter_key="review_final",
        tools_enabled=["web_search", "web_fetch", "read_file", "write_file", "list_files"],
    )
    out = report_dir / "final_review.md"
    if not out.exists():
        _log("review-final", "⚠ final_review.md 未生成（不阻塞渲染）")
        return False
    _log("review-final", "✓ final_review.md 已生成")
    return True


def stage_render(report_dir: Path, dim_ids: list[str]) -> bool:
    """§5.12 render → report.md + citations.json。"""
    _log("render", "渲染最终报告（引用编号 + TOC + 参考文献）")
    s_full = report_dir / "sections" / "s_full.md"
    outline = report_dir / "outline.json"
    ev_paths = [report_dir / "sub_reports" / f"{d}.evidence.json" for d in dim_ids]
    output = report_dir / "report.md"
    res = render_report(s_full, outline, ev_paths, output)
    if not res.get("ok", True) and res.get("errors"):
        _log("render", f"✗ 渲染失败：{json.dumps(res.get('errors', [])[:3], ensure_ascii=False)}")
        return False
    orphan = res.get("orphan_citations", [])
    unresolved = res.get("claim_id_leakage", {}).get("unresolved", [])
    if orphan or unresolved:
        _log("render", f"⚠ 渲染完成但有缺陷：orphan={len(orphan)}, unresolved_claim_leak={len(unresolved)}（继续交付）")
    else:
        _log("render", "✓ 渲染完成，无 orphan / unresolved leakage")
    return output.exists()


# ── 主流水线 ────────────────────────────────────────────────────────────
def run_pipeline(query: str, *, force_mode: str = "normal",
                 emit=None) -> dict | None:
    """跑 normal 模式完整流水线。返回结果字典或 None。

    Args:
        query: 研究需求文本
        force_mode: 强制使用的档位（quick/normal/heavy），默认 normal
        emit: 事件回调 (event_type, data)，供 web 端订阅进度
    Returns:
        dict with keys: report_dir, report_md, report_html, outline,
        all_evidence, final_review, pipeline_log, elapsed_seconds
    """
    # 设置 emit 回调
    if emit:
        llm.set_emit_callback(emit)
    start = time.time()
    report_dir = make_report_dir(query)
    _log("controller", f"报告目录：{report_dir}")
    _emit("task_start", {"report_dir": str(report_dir), "query": query[:200]})

    pipeline_log = report_dir / "pipeline.log"
    def write_log(stage: str, status: str, extra: str = "") -> None:
        with pipeline_log.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {stage} | {status} {extra}\n")

    # ── 1. scout ──────────────────────────────────────────────────────────
    _emit("phase_change", {"phase": "scout", "name": "预研侦察"})
    if not stage_scout(query, report_dir):
        write_log("scout", "FAIL")
        _emit("error", {"stage": "scout", "message": "scout 失败"})
        return None
    briefing = json.loads((report_dir / "briefing.json").read_text(encoding="utf-8"))
    mode = force_mode or briefing.get("recommended_mode", "normal")
    write_log("scout", "OK", f"mode={mode}")
    _emit("scout_done", {"mode": mode, "briefing": briefing})

    # ── 2. plan ───────────────────────────────────────────────────────────
    _emit("phase_change", {"phase": "plan", "name": "研究规划"})
    plan = stage_plan(query, report_dir, mode)
    if plan is None:
        write_log("plan", "FAIL")
        _emit("error", {"stage": "plan", "message": "plan 失败"})
        return None
    write_log("plan", "OK", f"{len(plan['dimensions'])} dims")
    _emit("plan_done", {"dimensions": plan.get("dimensions", [])})

    # ── 3. research × N + validator + review(子) ─────────────────────────
    dim_ids = []
    all_evidence = []
    for dim in plan["dimensions"]:
        dim_id = dim["id"]
        dim_ids.append(dim_id)
        dim_name = dim.get("name", dim_id)
        _emit("phase_change", {"phase": "research", "name": f"维度调研 - {dim_name}",
                               "dimension": dim_name})
        # research（含 1 次失败重试，见 §4.3）
        ok = stage_research_one(query, report_dir, dim)
        if not ok:
            write_log(f"research/{dim_id}", "FAIL-initial")
            _emit("error", {"stage": "research", "dimension": dim_id,
                            "message": f"{dim_id} research 初始失败，尝试降级重试"})
            # 降级重试：把 depth 降到 moderate，再试一次
            dim_retry = dict(dim)
            dim_retry["depth"] = "moderate"
            ok = stage_research_one(query, report_dir, dim_retry)
            if not ok:
                _log("controller", f"⚠ {dim_id} 降级重试仍失败，跳过该维度，继续后续维度")
                write_log(f"research/{dim_id}", "FAIL-skip")
                _emit("error", {"stage": "research", "dimension": dim_id,
                                "message": f"{dim_id} 研究失败，已跳过"})
                dim_ids.pop()
                continue
        # validator（失败 1 次回 research 修复）
        vres = validate_evidence(report_dir / "sub_reports" / f"{dim_id}.evidence.json")
        if vres.get("ok"):
            stats = vres.get("stats", {})
            _log("ev-validator", f"✓ {dim_id} 通过：{stats}")
            _emit("validator_done", {"dimension": dim_id, "status": "pass", "stats": stats})
        else:
            errs = vres.get("errors", [])
            _log("controller", f"{dim_id} validator 失败，回 research 修复一次（§4.3）")
            _emit("validator_done", {"dimension": dim_id, "status": "fail_repairing",
                                      "errors": errs[:3]})
            write_log(f"ev-validator/{dim_id}", "FAIL-1", json.dumps(errs[:3], ensure_ascii=False))
            ok2 = stage_research_one(query, report_dir, dim, validator_errors=errs)
            if not ok2 or not stage_evidence_validator(report_dir, dim_id):
                _log("controller", f"⚠ {dim_id} 修复后仍失败，超预算跳过该维度")
                write_log(f"research/{dim_id}", "FAIL-after-retry")
                dim_ids.pop()
                continue
        write_log(f"research/{dim_id}", "OK")
        # 读 evidence 用于后续结果返回
        try:
            ev = json.loads((report_dir / "sub_reports" / f"{dim_id}.evidence.json").read_text(encoding="utf-8"))
            all_evidence.append(ev)
        except Exception:
            pass
        # review 子报告（不阻塞）
        stage_review_sub(query, report_dir, dim)

    if not dim_ids:
        _log("controller", "✗ 所有维度研究失败")
        _emit("error", {"stage": "research", "message": "所有维度研究失败"})
        return None

    _emit("research_all_done", {"dimensions": dim_ids,
                                "total_claims": sum(len(e.get("claims", [])) for e in all_evidence),
                                "total_sources": sum(len(e.get("sources", [])) for e in all_evidence)})

    # ── 4. report-planner + outline validator ─────────────────────────────
    _emit("phase_change", {"phase": "report_plan", "name": "报告规划"})
    planner_ok = stage_report_planner(query, report_dir, dim_ids)
    if not planner_ok:
        _log("controller", "report-planner 未产出 outline，瞬时失败重试一次")
        planner_ok = stage_report_planner(query, report_dir, dim_ids)
    if not planner_ok:
        write_log("report-planner", "FAIL")
        _emit("error", {"stage": "report_planner", "message": "report-planner 失败"})
        return None
    section_ids = stage_outline_validator(report_dir, dim_ids)
    if section_ids is None:
        for retry in (1, 2):
            _log("controller", f"outline validator 失败，回 planner 修复（{retry}/2）")
            _emit("phase_change", {"phase": "report_plan", "name": f"报告规划修复({retry}/2)"})
            if not stage_report_planner(query, report_dir, dim_ids):
                continue
            section_ids = stage_outline_validator(report_dir, dim_ids)
            if section_ids is not None:
                break
    if section_ids is None:
        write_log("outline-validator", "FAIL")
        _emit("error", {"stage": "outline_validator", "message": "outline 校验失败"})
        return None
    write_log("report-planner", "OK", f"{len(section_ids)} sections")
    _emit("report_planner_done", {"sections": section_ids})

    # ── 5. report-writer（full_outline） ──────────────────────────────────
    _emit("phase_change", {"phase": "report_write", "name": "报告撰写"})
    if not stage_report_writer(query, report_dir):
        write_log("report-writer", "FAIL")
        _emit("error", {"stage": "report_writer", "message": "report-writer 失败"})
        return None
    write_log("report-writer", "OK")
    _emit("report_writer_done", {})

    # ── 6. 终稿 review（normal 版） ────────────────────────────────────────
    _emit("phase_change", {"phase": "review", "name": "终稿审查"})
    stage_review_final(query, report_dir, dim_ids)
    write_log("review-final", "OK")

    # ── 7. render ─────────────────────────────────────────────────────────
    _emit("phase_change", {"phase": "render", "name": "渲染报告"})
    if not stage_render(report_dir, dim_ids):
        write_log("render", "FAIL")
        # 渲染失败也保留 s_full.md 作为产物
    else:
        write_log("render", "OK")

    elapsed = time.time() - start
    _log("controller", f"✓ 流水线完成，耗时 {elapsed/60:.1f} 分钟")
    _log("controller", f"报告目录：{report_dir}")
    _log("controller", f"终稿：{report_dir / 'report.md'}")

    # 读取最终产物
    report_md = ""
    report_md_path = report_dir / "report.md"
    if report_md_path.exists():
        report_md = report_md_path.read_text(encoding="utf-8")
    elif (report_dir / "sections" / "s_full.md").exists():
        report_md = (report_dir / "sections" / "s_full.md").read_text(encoding="utf-8")

    outline = {}
    try:
        outline = json.loads((report_dir / "outline.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    final_review_text = ""
    try:
        final_review_text = (report_dir / "final_review.md").read_text(encoding="utf-8")
    except Exception:
        pass

    result = {
        "report_dir": str(report_dir),
        "report_md": report_md,
        "report_md_path": str(report_md_path),
        "outline": outline,
        "all_evidence": all_evidence,
        "final_review": final_review_text,
        "pipeline_log": str(pipeline_log),
        "elapsed_seconds": elapsed,
        "dim_ids": dim_ids,
        "mode": mode,
        "briefing": briefing,
    }
    _emit("complete", {"report_dir": str(report_dir), "elapsed_seconds": elapsed})
    return result


# ── Demo 入口 ───────────────────────────────────────────────────────────
def main() -> int:
    query = "以商汤科技（SenseTime, 0020.HK）作为公司深度研究标的，进行系统性的公司深度分析报告。研究应覆盖：公司基本面与历史沿革、业务板块与商业模式、核心技术与产品（含日日新大模型体系）、财务表现与经营数据、行业地位与竞争格局（与旷视、第四范式、海康威视等对比）、风险因素与监管环境、未来增长曲线与战略展望。报告需基于真实公开信息，所有结论均可溯源。"
    result = run_pipeline(query, force_mode="normal")
    if result is None:
        print("\n✗ 流水线失败", file=sys.stderr)
        return 1
    print(f"\n✓ 报告目录：{result['report_dir']}")
    print(f"✓ 终稿：{result['report_md_path']}")
    print(f"✓ 报告长度：{len(result['report_md'])} 字符")
    print(f"✓ 耗时：{result['elapsed_seconds']/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
