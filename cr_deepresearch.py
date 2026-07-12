"""CR-Agent 适配层：桥接 web 端接口到 cr_agent 自研流水线。

这是从 0-1 设计的自研公司调研智能体，所有数据均来自真实
web_search/web_fetch，严禁模拟数据。

流水线（5 角色 + 内置 render）：
  scout → planner → researcher(×N) → writer(×N sections) → reviewer → render(内置)

设计创新点（vs sn-deepresearch）：
- EvidenceCard 增量追加（JSONL），而非一次性写大 JSON
- 预算前置约束（Budget 在角色派发前设置）
- Section-by-section 写作（writer 按章节逐段派发）
- Controller 内置 render（[card:dN.cM] → [N] 引用替换 + TOC + 来源列表）

核心文件：
- cr_agent/controller.py: 流水线编排
- cr_agent/llm.py: LLM 智能体循环（function-calling + 工具 + 预算）
- cr_agent/tools.py: 工具层（web_search/web_fetch/add_card/write_section/...）
- cr_agent/prompts/: 5 角色 prompt
"""
import json
import os
import sys
import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable

# 把 cr_agent 包加入 sys.path
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from cr_agent import controller, llm

logger = logging.getLogger(__name__)


# ============================================================
# 任务管理（兼容 app.py 的 import，实际不使用——app.py 自己管理任务）
# ============================================================

_tasks: Dict[str, Dict[str, Any]] = {}


def create_task() -> str:
    import uuid
    task_id = f"cr_{uuid.uuid4().hex[:12]}"
    _tasks[task_id] = {"id": task_id, "status": "pending", "created_at": time.time(),
                       "events": [], "result": None}
    return task_id


def get_task(task_id: str) -> Optional[Dict]:
    return _tasks.get(task_id)


def update_task(task_id: str, **kwargs):
    if task_id in _tasks:
        _tasks[task_id].update(kwargs)


def add_event(task_id: str, event_type: str, data: Dict):
    if task_id in _tasks:
        _tasks[task_id]["events"].append({
            "type": event_type, "data": data, "timestamp": time.time(),
        })


def list_tasks(limit: int = 20) -> List[Dict]:
    return sorted(_tasks.values(), key=lambda t: t.get("created_at", 0), reverse=True)[:limit]


# ============================================================
# 主入口：运行 cr-agent 流水线
# ============================================================

def run_cr_deepresearch(
    api_key: str,
    company_name: str,
    depth: str = "normal",
    model: Optional[str] = None,
    emit: Optional[Callable[[str, Dict], None]] = None,
) -> Dict[str, Any]:
    """运行 cr_agent 自研公司深度分析流水线。

    Args:
        api_key: SenseNova API Key
        company_name: 目标公司名称
        depth: 研究档位 (quick/normal/heavy)
        model: 模型名称，默认 sensenova-6.7-flash-lite
        emit: 事件回调 (event_type, data)

    Returns:
        完整研究结果字典，包含 final_report / executive_summary / logs 等
    """
    def _emit(event_type: str, data: Dict):
        if emit:
            try:
                emit(event_type, data)
            except Exception as e:
                logger.error(f"Emit event failed: {event_type}, {e}")

    start_time = time.time()

    # ── 配置 LLM 客户端（用户自定义 key/model） ──────────────────────────
    use_model = model or "sensenova-6.7-flash-lite"
    llm.configure(api_key=api_key, model=use_model)

    # ── 构建研究需求 ──────────────────────────────────────────────────────
    query = _build_query(company_name)

    _emit("task_start", {
        "company": company_name,
        "depth": depth,
        "mode": "cr-agent",
        "model": use_model,
        "estimated_duration": "约8-20分钟（真实联网调研）",
    })

    # ── 运行流水线 ────────────────────────────────────────────────────────
    try:
        result = controller.run_pipeline(
            query,
            depth=depth,
            emit=_emit,
        )
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        _emit("error", {"message": f"流水线异常：{e}"})
        return _build_error_result(company_name, depth, use_model, str(e), start_time)

    if result is None:
        _emit("error", {"message": "流水线未产出结果"})
        return _build_error_result(company_name, depth, use_model, "流水线未产出结果", start_time)

    if result.get("error"):
        _emit("error", {"message": result["error"]})
        return _build_error_result(company_name, depth, use_model, result["error"], start_time)

    # ── 构建执行摘要 ────────────────────────────────────────────────────────
    briefing = result.get("briefing", {})
    plan = result.get("plan", {})
    review = result.get("review", {})
    dim_ids = result.get("dim_ids", [])
    section_ids = result.get("section_ids", [])
    executive_summary = _build_executive_summary(
        company_name, depth, use_model, briefing, plan, review, dim_ids, section_ids,
        result.get("elapsed_seconds", 0)
    )

    # ── 构建 logs（从 pipeline.log 读取） ────────────────────────────────
    logs = _build_logs_from_pipeline(result.get("pipeline_log", ""))

    # ── 构建最终结果 ────────────────────────────────────────────────────────
    final_report = result.get("report_md", "")
    elapsed_ms = int((time.time() - start_time) * 1000)

    _emit("complete", {
        "report": final_report,
        "summary": executive_summary,
        "total_duration_ms": elapsed_ms,
        "report_length": len(final_report),
    })

    return {
        "company": company_name,
        "depth": depth,
        "mode": "cr-agent",
        "model": use_model,
        "final_report": final_report,
        "executive_summary": executive_summary,
        "key_metrics": [],
        "logs": logs,
        "total_duration_ms": elapsed_ms,
        "review": {"final_review": review, "verdict": review.get("verdict", "pass")},
        "fact_check": {},
        "report_dir": result.get("report_dir", ""),
        "report_md_path": result.get("report_md_path", ""),
        "briefing": briefing,
        "plan": plan,
        "dim_ids": dim_ids,
        "section_ids": section_ids,
    }


def _build_query(company_name: str) -> str:
    """从公司名构建研究需求文本。"""
    return (
        f"以{company_name}作为公司深度研究标的，进行系统性的公司深度分析报告。"
        f"研究应覆盖：公司基本面与历史沿革、业务板块与商业模式、核心技术与产品、"
        f"财务表现与经营数据、行业地位与竞争格局、风险因素与监管环境、"
        f"未来增长曲线与战略展望。报告需基于真实公开信息，所有结论均可溯源。"
    )


def _build_executive_summary(
    company_name: str, depth: str, model: str,
    briefing: Dict, plan: Dict, review: Dict,
    dim_ids: List[str], section_ids: List[str], elapsed_seconds: float,
) -> str:
    """构建执行摘要。"""
    industry = briefing.get("industry", "未知")
    company_summary = briefing.get("company_summary", "")
    listed = briefing.get("listed", "")
    dims = plan.get("dimensions", [])
    outline = plan.get("outline", [])
    verdict = review.get("verdict", "unknown")
    score = review.get("overall_score", "?")
    gaps = review.get("gaps", [])
    suggestions = review.get("suggestions", [])

    minutes = int(elapsed_seconds // 60)
    seconds = int(elapsed_seconds % 60)

    dims_text = "\n".join(
        f"- **{d.get('name', d.get('id',''))}**：{', '.join(d.get('key_questions', [])[:2])}..."
        for d in dims
    ) if dims else "- 无维度数据"

    sections_text = "\n".join(
        f"- {s.get('title', s.get('section_id',''))}"
        for s in outline
    ) if outline else "- 无大纲数据"

    gaps_text = "\n".join(f"- {g.get('issue', str(g))}" for g in gaps[:3]) if gaps else "- 无重大缺口"
    suggestions_text = "\n".join(f"- {s}" for s in suggestions[:3]) if suggestions else "- 无建议"

    return f"""# {company_name} 深度研究摘要

**研究档位**：{depth} | **模型**：{model} | **耗时**：{minutes}分{seconds}秒
**行业**：{industry} | **上市状态**：{listed}
**证据规模**：{len(dim_ids)} 个维度成功调研 / {len(section_ids)} 个章节生成

## 公司定位

{company_summary}

## 研究维度

{dims_text}

## 报告大纲

{sections_text}

## 质量审查

- **verdict**：{verdict}
- **overall_score**：{score}
- **关键缺口**：
{gaps_text}
- **改进建议**：
{suggestions_text}

> 本报告由 cr_agent 自研多 Agent 系统自动生成，基于真实联网调研。
> 所有数据均可溯源至真实公开来源，无模拟数据。"""


def _build_logs_from_pipeline(pipeline_log_path: str) -> List[Dict]:
    """从 pipeline.log 构建日志列表。"""
    if not pipeline_log_path or not os.path.exists(pipeline_log_path):
        return []
    logs = []
    try:
        with open(pipeline_log_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                # 格式: HH:MM:SS stage | status extra
                parts = line.split(" | ", 1)
                ts_stage = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                ts_parts = ts_stage.split(" ", 1)
                ts = ts_parts[0] if ts_parts else ""
                stage = ts_parts[1] if len(ts_parts) > 1 else ""
                logs.append({
                    "step": i,
                    "agent": stage,
                    "action": rest[:200],
                    "level": "info" if "OK" in rest or "✓" in rest else "warning" if "⚠" in rest else "info",
                    "timestamp": ts,
                    "timestamp_unix": time.time(),
                })
    except Exception as e:
        logger.warning(f"Failed to read pipeline log: {e}")
    return logs


def _build_error_result(company_name: str, depth: str, model: str,
                         error_msg: str, start_time: float) -> Dict[str, Any]:
    """构建错误结果。"""
    elapsed_ms = int((time.time() - start_time) * 1000)
    return {
        "company": company_name,
        "depth": depth,
        "mode": "cr-agent",
        "model": model,
        "final_report": "",
        "executive_summary": f"研究失败：{error_msg}",
        "key_metrics": [],
        "logs": [],
        "total_duration_ms": elapsed_ms,
        "review": {"verdict": "fail", "error": error_msg},
        "fact_check": {},
        "error": error_msg,
    }
