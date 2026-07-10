"""
多Agent深度研究系统 - Flask 应用入口（Vercel 零配置部署）

包含两套 Agent 系统：
- classic: 自研多 Agent 系统（Planner/Researcher/Writer/Reviewer/FactChecker）
- sn-deepresearch: 基于 sn-deep-research 设计思想的复刻版

API 端点：
- GET  /api/health - 健康检查
- POST /api/research - 启动研究任务
- GET  /api/research/<id>/stream - SSE实时流
- GET  /api/research/<id>/result - 获取研究结果
- GET  /api/tasks - 任务列表（调试用）
- GET  /api/models - 可用模型列表

部署方式：Vercel 零配置 Flask 部署
- 根目录的 app.py 会被 Vercel 自动识别
- public/ 目录下的文件作为静态资源
- requirements.txt 放根目录
"""

import json
import os
import sys
import time
import uuid
import threading
import queue
import logging
from datetime import datetime

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

from agent_system import ResearchOrchestrator
from sn_deepresearch import run_sn_deepresearch, create_task as sn_create_task, get_task as sn_get_task, update_task as sn_update_task, add_event as sn_add_event, list_tasks as sn_list_tasks

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("api")

# ============================================================
# 可用模型配置
# ============================================================

AVAILABLE_MODELS = [
    {
        "id": "sensenova-6.7-flash-lite",
        "name": "SenseNova 6.7 Flash Lite",
        "description": "轻量快速版，适合日常使用",
        "default": True,
    },
    {
        "id": "sensenova-6.7-flash",
        "name": "SenseNova 6.7 Flash",
        "description": "标准版，平衡速度和质量",
        "default": False,
    },
]

# ============================================================
# Flask 应用初始化
# ============================================================

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Vercel 需要的 WSGI application 变量
application = app


# ============================================================
# 静态页面路由
# ============================================================

@app.route("/")
@app.route("/index.html")
def index():
    """
    首页 - 返回前端页面

    优先从 public/ 目录读取，兼容 Vercel 静态文件服务和 Flask 直接服务两种模式
    """
    # 尝试多个可能的 index.html 路径
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "index.html"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
    ]
    for path in possible_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>DeepResearch Agent</h1><p>页面文件未找到</p>", 404

# ============================================================
# 全局任务存储（内存字典）
# ============================================================
# 注意：Serverless 环境下每次请求可能是不同实例，内存存储不可靠
# 生产环境建议使用外部存储（如 Redis、Vercel KV 等）

tasks = {}
tasks_lock = threading.Lock()

# 任务保留时间（秒），超时自动清理
TASK_TTL = 3600  # 1小时

# 最大任务数量上限（防止内存无限增长）
MAX_TASKS = 100

# 总研究超时（秒），防止任务无限运行
TOTAL_TIMEOUT = 600  # 10分钟


def generate_task_id() -> str:
    """生成唯一任务ID"""
    return uuid.uuid4().hex[:16]


def clean_expired_tasks():
    """清理过期任务"""
    now = time.time()
    with tasks_lock:
        expired_ids = [
            tid for tid, task in tasks.items()
            if now - task.get("created_at", 0) > TASK_TTL
        ]
        for tid in expired_ids:
            del tasks[tid]
        if expired_ids:
            logger.info(f"Cleaned {len(expired_ids)} expired tasks")


def mask_api_key(api_key: str) -> str:
    """安全脱敏API Key，仅保留前4后4位"""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return api_key[:4] + "****" + api_key[-4:]


# ============================================================
# API 端点
# ============================================================

@app.route("/api/health", methods=["GET"])
def health_check():
    """
    健康检查端点

    Returns:
        服务状态信息
    """
    with tasks_lock:
        task_count = len(tasks)
        running_count = sum(1 for t in tasks.values() if t["status"] == "running")

    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": "3.0.0",
        "total_tasks": task_count,
        "running_tasks": running_count,
        "agents": {
            "classic": ["planner", "researcher", "writer", "reviewer", "fact_checker"],
            "sn-deepresearch": ["scout", "planner", "researcher", "reviewer", "report_planner", "report_writer", "fact_checker"],
        },
        "deployment": "vercel-serverless",
    })


@app.route("/api/models", methods=["GET"])
def get_models():
    """
    获取可用模型列表

    Returns:
        可用模型列表
    """
    return jsonify({
        "models": AVAILABLE_MODELS,
        "default_model": "sensenova-6.7-flash-lite",
    })


@app.route("/api/research", methods=["POST"])
def start_research():
    """
    启动深度研究任务

    Request Body:
        company_name: 公司名称
        depth: 研究深度
               - classic模式: basic/standard/deep
               - sn-deepresearch模式: quick/normal/heavy
        api_key: SenseNova API Key
        agent_mode: 代理模式 (classic / sn-deepresearch)，默认 classic
        model: 模型名称，默认 sensenova-6.7-flash-lite

    Returns:
        任务ID和状态信息
    """
    data = request.get_json(force=True, silent=True) or {}

    company_name = (data.get("company_name") or "").strip()
    depth = (data.get("depth") or "").strip().lower()
    api_key = (data.get("api_key") or "").strip()
    agent_mode = (data.get("agent_mode") or "classic").strip().lower()
    model = (data.get("model") or "").strip()

    # 参数校验
    if not company_name:
        return jsonify({"error": "公司名称不能为空"}), 400

    # 校验 agent_mode
    if agent_mode not in ("classic", "sn-deepresearch"):
        return jsonify({"error": "agent_mode 必须是 classic 或 sn-deepresearch"}), 400

    # 根据模式校验 depth
    if agent_mode == "classic":
        valid_depths = ("basic", "standard", "deep")
        if not depth:
            depth = "basic"
    else:
        valid_depths = ("quick", "normal", "heavy")
        if not depth:
            depth = "normal"

    if depth not in valid_depths:
        return jsonify({"error": f"depth 参数必须是 {', '.join(valid_depths)}"}), 400

    # 校验 model
    if not model:
        model = "sensenova-6.7-flash-lite"
    valid_model_ids = [m["id"] for m in AVAILABLE_MODELS]
    if model not in valid_model_ids:
        # 允许自定义模型名（用户可能有其他模型）
        pass

    # 优先使用请求中的API Key，其次使用环境变量
    if not api_key:
        api_key = os.environ.get("SENSENOVA_API_KEY", "")

    if not api_key:
        return jsonify({
            "error": "API Key不能为空，请在页面中输入或配置环境变量 SENSENOVA_API_KEY"
        }), 400

    # 清理过期任务
    clean_expired_tasks()

    # 检查任务数量上限
    with tasks_lock:
        if len(tasks) >= MAX_TASKS:
            return jsonify({"error": f"任务数量已达上限（{MAX_TASKS}），请稍后再试"}), 503

    task_id = generate_task_id()

    # 创建任务
    task = {
        "id": task_id,
        "company_name": company_name,
        "depth": depth,
        "agent_mode": agent_mode,
        "model": model,
        "status": "running",
        "api_key_masked": mask_api_key(api_key),
        "created_at": time.time(),
        "result": None,
        "error": None,
        "events": [],
        "event_queue": queue.Queue(),
    }

    with tasks_lock:
        tasks[task_id] = task

    logger.info(f"Research task created: {task_id}, company={company_name}, depth={depth}, mode={agent_mode}, model={model}")

    # 后台线程执行研究任务
    def run_research():
        try:
            logger.info(f"Task {task_id}: Starting research for '{company_name}' with depth '{depth}', mode '{agent_mode}'")

            def event_callback(event_type: str, event_data: dict):
                event = {
                    "type": event_type,
                    "data": event_data,
                    "timestamp": datetime.now().isoformat(),
                }
                with tasks_lock:
                    task["events"].append(event)
                task["event_queue"].put(event)

            # 根据模式执行不同的研究流程
            if agent_mode == "classic":
                # 经典自研模式
                orchestrator = ResearchOrchestrator()
                result = orchestrator.run(
                    company_name=company_name,
                    depth=depth,
                    api_key=api_key,
                    model=model,
                    event_callback=event_callback,
                    total_timeout=TOTAL_TIMEOUT,
                )
            else:
                # SN-DeepResearch 模式
                result = run_sn_deepresearch(
                    api_key=api_key,
                    company_name=company_name,
                    depth=depth,
                    model=model,
                    emit=event_callback,
                )

            # 保存结果
            with tasks_lock:
                task["status"] = "completed"
                task["result"] = result
                task["completed_at"] = time.time()

            # 发送完成事件
            complete_event = {
                "type": "complete",
                "data": {
                    "report": result.get("final_report", result.get("report", "")),
                    "summary": result.get("executive_summary", result.get("summary", "")),
                    "key_metrics": result.get("key_metrics", []),
                    "logs": result.get("logs", []),
                    "total_duration_ms": result.get("total_duration_ms", 0),
                    "review": result.get("review", {}),
                    "fact_check": result.get("fact_check", {}),
                    "agent_mode": agent_mode,
                    "model": model,
                },
                "timestamp": datetime.now().isoformat(),
            }
            with tasks_lock:
                task["events"].append(complete_event)
            task["event_queue"].put(complete_event)
            task["event_queue"].put({"type": "__end__", "data": {}})

            logger.info(f"Task {task_id}: Completed with status 'completed'")

        except Exception as e:
            logger.error(f"Task {task_id}: Failed with error: {e}", exc_info=True)
            with tasks_lock:
                task["status"] = "error"
                task["error"] = str(e)
                task["completed_at"] = time.time()

            error_event = {
                "type": "error",
                "data": {"message": str(e)},
                "timestamp": datetime.now().isoformat(),
            }
            with tasks_lock:
                task["events"].append(error_event)
            task["event_queue"].put(error_event)
            task["event_queue"].put({"type": "__end__", "data": {}})

    thread = threading.Thread(target=run_research, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "task_id": task_id,
        "company_name": company_name,
        "depth": depth,
        "agent_mode": agent_mode,
        "model": model,
        "status": "running",
        "message": "研究任务已启动",
    }), 201


@app.route("/api/research/<task_id>/stream", methods=["GET"])
def research_stream(task_id: str):
    """
    SSE实时流端点

    注意：Vercel Serverless Function 有执行时间限制
    - 免费版：10秒
    - Pro版：300秒
    研究任务可能超过这个时间，SSE连接会被中断。
    前端应实现重连机制，或使用 /result 端点轮询。

    Args:
        task_id: 任务ID

    Returns:
        SSE流式响应
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    def generate():
        """生成SSE事件流"""
        # 先发送已有事件（重连场景）
        with tasks_lock:
            past_events = list(task["events"])

        sent_count = 0
        for event in past_events:
            if event["type"] == "__end__":
                continue
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            sent_count += 1

        # 如果任务已完成，直接发送complete事件并结束
        if task["status"] in ("completed", "error"):
            # 检查是否已经发送过complete事件
            has_complete = any(
                e["type"] == "complete" for e in past_events
            )
            if not has_complete and task.get("result"):
                result = task["result"]
                complete_event = {
                    "type": "complete",
                    "data": {
                        "report": result.get("report", ""),
                        "summary": result.get("summary", ""),
                        "key_metrics": result.get("key_metrics", []),
                        "logs": result.get("logs", []),
                        "total_duration_ms": result.get("total_duration_ms", 0),
                        "review": result.get("review", {}),
                        "fact_check": result.get("fact_check", {}),
                    },
                    "timestamp": datetime.now().isoformat(),
                }
                yield f"data: {json.dumps(complete_event, ensure_ascii=False)}\n\n"
            return

        # 继续监听新事件
        event_queue = task["event_queue"]
        # 设置较短的超时，避免超过 Vercel 函数执行时间限制
        # 每 5 秒发送一次心跳，给前端重连的机会
        while True:
            try:
                event = event_queue.get(timeout=5)
                if event["type"] == "__end__":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except queue.Empty:
                # 超时，检查任务状态
                with tasks_lock:
                    current_task = tasks.get(task_id)
                if current_task and current_task["status"] in ("completed", "error"):
                    break
                # 发送心跳，保持连接活跃
                yield f"data: {json.dumps({'type': 'ping', 'data': {}, 'timestamp': datetime.now().isoformat()}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.route("/api/research/<task_id>/result", methods=["GET"])
def research_result(task_id: str):
    """
    获取研究结果（轮询用）

    Args:
        task_id: 任务ID

    Returns:
        任务状态和结果
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    response = {
        "task_id": task_id,
        "company_name": task["company_name"],
        "depth": task["depth"],
        "status": task["status"],
        "created_at": datetime.fromtimestamp(task["created_at"]).isoformat(),
        "api_key_masked": task["api_key_masked"],
    }

    if task["status"] == "completed":
        result = task["result"]
        response["result"] = {
            "summary": result.get("summary", ""),
            "key_metrics": result.get("key_metrics", []),
            "report": result.get("report", ""),
            "logs": result.get("logs", []),
            "total_duration_ms": result.get("total_duration_ms", 0),
            "review": result.get("review", {}),
            "fact_check": result.get("fact_check", {}),
        }
        response["completed_at"] = datetime.fromtimestamp(task["completed_at"]).isoformat()
    elif task["status"] == "error":
        response["error"] = task["error"]
        response["completed_at"] = datetime.fromtimestamp(task["completed_at"]).isoformat()
    else:
        # running 状态，返回已有的事件数和进度
        with tasks_lock:
            events = list(task["events"])
        response["events_count"] = len(events)
        # 从事件中提取最新进度
        for event in reversed(events):
            if event["type"] == "progress":
                response["progress"] = event["data"]
                break

    return jsonify(response)


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    """
    列出所有任务（调试用）

    Returns:
        任务列表
    """
    with tasks_lock:
        task_list = []
        for tid, task in tasks.items():
            task_list.append({
                "id": tid,
                "company_name": task["company_name"],
                "depth": task["depth"],
                "status": task["status"],
                "created_at": datetime.fromtimestamp(task["created_at"]).isoformat(),
                "api_key_masked": task["api_key_masked"],
            })

    return jsonify({
        "success": True,
        "total": len(task_list),
        "tasks": sorted(task_list, key=lambda t: t["created_at"], reverse=True),
    })


# ============================================================
# 启动服务
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("多Agent深度研究系统 v2.1 - Vercel Flask 部署")
    print("=" * 60)
    print("Agent配置：")
    print("  1. Planner      - 研究总监     (web_search)")
    print("  2. Researcher   - 信息检索专家  (web_search, company_lookup, financial_data)")
    print("  3. Writer       - 资深研究员    (无工具)")
    print("  4. Reviewer     - 质量审核专家  (无工具)")
    print("  5. FactChecker  - 事实核查员    (web_search)")
    print("=" * 60)
    print("API端点：")
    print("  GET    /api/health              - 健康检查")
    print("  POST   /api/research            - 启动研究")
    print("  GET    /api/research/<id>/stream - SSE实时流")
    print("  GET    /api/research/<id>/result - 获取结果")
    print("  GET    /api/tasks               - 任务列表")
    print("=" * 60)
    print("研究深度：")
    print("  basic    - 3维度, 审核不修改, 基础事实核查 - 约90秒")
    print("  standard - 5维度, 审核+修改1轮, LLM事实核查 - 约3分钟")
    print("  deep     - 7维度, 审核+修改2轮, 全量事实核查 - 约5分钟")
    print("=" * 60)
    print("提示：")
    print("  - 生产环境部署到 Vercel 后，通过 /api/ 路径访问")
    print("  - 本地开发运行：python app.py")
    print("  - 前端页面：public/index.html")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
