"""
多Agent深度研究系统 - Vercel Serverless Function 入口

5个API端点：
- GET  /api/health - 健康检查
- POST /api/research - 启动研究任务
- GET  /api/research/<id>/stream - SSE实时流
- GET  /api/research/<id>/result - 获取研究结果
- GET  /api/tasks - 任务列表（调试用）

注意：
- Vercel Serverless Function 使用 WSGI handler 运行 Flask 应用
- 此文件需要导出 `app` 和 `application` 变量
- 免费版函数最大执行时间为 10 秒，Pro版为 300 秒
- SSE 在 Serverless 环境下有超时限制，前端应实现重连和轮询降级
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

# 确保同目录下的模块可以被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_system import ResearchOrchestrator

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("api")

# ============================================================
# Flask 应用初始化
# ============================================================

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Vercel 需要的 WSGI application 变量
application = app


def add_route(rule: str, **options):
    """
    同时注册带 /api 前缀和不带前缀的路由，
    确保在 Vercel（可能剥离/api前缀）和本地开发（完整路径）都能工作。
    """
    def decorator(f):
        # 带 /api 前缀的路由（本地开发用）
        app.add_url_rule(f"/api{rule}", f"api_{f.__name__}", f, **options)
        # 不带前缀的路由（Vercel 环境下可能用到）
        app.add_url_rule(rule, f.__name__, f, **options)
        return f
    return decorator

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


# ============================================================
# 工具函数
# ============================================================

def generate_task_id() -> str:
    """生成唯一任务ID"""
    return uuid.uuid4().hex[:16]


def clean_expired_tasks() -> None:
    """清理过期任务（在任务创建时调用一次，不单独启动线程）"""
    now = time.time()
    with tasks_lock:
        expired = []
        for tid, task in tasks.items():
            created = task.get("created_at_ts", 0)
            if now - created > TASK_TTL:
                expired.append(tid)
        for tid in expired:
            del tasks[tid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired tasks")


def create_task(company_name: str, depth: str, api_key: str) -> str:
    """
    创建新任务并启动研究

    Args:
        company_name: 公司名称
        depth: 研究深度
        api_key: API密钥

    Returns:
        任务ID
    """
    # 清理过期任务
    clean_expired_tasks()

    # 检查任务数量上限
    with tasks_lock:
        if len(tasks) >= MAX_TASKS:
            raise RuntimeError(f"任务数量已达上限（{MAX_TASKS}），请稍后再试")

    task_id = generate_task_id()
    event_queue = queue.Queue()
    now = datetime.now()

    task = {
        "id": task_id,
        "company_name": company_name,
        "depth": depth,
        "status": "running",  # running / completed / error
        "result": None,
        "event_queue": event_queue,
        "events": [],  # 所有事件记录
        "created_at": now.isoformat(),
        "created_at_ts": time.time(),
        "started_at": now.isoformat(),
        "completed_at": None,
        "api_key_masked": mask_api_key(api_key),  # 仅用于日志展示，不存完整key
    }

    with tasks_lock:
        tasks[task_id] = task

    # 安全注意：api_key 仅在内存中传递给后台线程，不持久化存储
    # 任务完成后，api_key 会随线程栈一起释放

    # 后台线程执行研究
    def event_callback(event_type: str, data: dict):
        """事件回调 - 将事件放入队列并保存"""
        event = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        event_queue.put(event)
        # 同时保存到事件列表（用于重连/轮询）
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["events"].append(event)

    def run_research():
        """后台执行研究任务"""
        try:
            logger.info(f"Task {task_id}: Starting research for '{company_name}' with depth '{depth}'")
            orchestrator = ResearchOrchestrator()
            result = orchestrator.run(
                company_name=company_name,
                depth=depth,
                api_key=api_key,
                event_callback=event_callback,
                total_timeout=TOTAL_TIMEOUT,
            )

            with tasks_lock:
                if task_id in tasks:
                    # 保存结果，但移除可能包含敏感信息的字段
                    safe_result = dict(result)
                    # 不保存 api_key 相关的任何信息
                    tasks[task_id]["result"] = safe_result
                    tasks[task_id]["status"] = "error" if result.get("error") else "completed"
                    tasks[task_id]["completed_at"] = datetime.now().isoformat()

            logger.info(f"Task {task_id}: Completed with status '{tasks[task_id]['status']}'")

        except Exception as e:
            logger.error(f"Task {task_id}: Failed with error: {e}", exc_info=True)
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]["status"] = "error"
                    tasks[task_id]["result"] = {"error": str(e)}
                    tasks[task_id]["completed_at"] = datetime.now().isoformat()

            # 发送错误事件
            error_event = {
                "type": "error",
                "data": {"message": str(e)},
                "timestamp": datetime.now().isoformat(),
            }
            event_queue.put(error_event)

        finally:
            # 发送哨兵事件表示结束
            event_queue.put({
                "type": "__end__",
                "data": {},
                "timestamp": datetime.now().isoformat(),
            })
            # 清理 api_key（确保不在内存中残留）
            # api_key 是函数参数，函数返回后自动释放

    thread = threading.Thread(target=run_research, daemon=True)
    thread.start()

    return task_id


def mask_api_key(api_key: str) -> str:
    """
    脱敏API Key，用于日志展示

    Args:
        api_key: 原始API Key

    Returns:
        脱敏后的API Key
    """
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return api_key[:4] + "****" + api_key[-4:]


# ============================================================
# API 端点
# ============================================================

@add_route("/health", methods=["GET"])
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
        "version": "2.0.0",
        "total_tasks": task_count,
        "running_tasks": running_count,
        "deployment": "vercel-serverless",
        "agents": ["planner", "researcher", "writer", "reviewer", "fact_checker"],
    })


@add_route("/research", methods=["POST"])
def start_research():
    """
    启动研究任务

    Request Body:
        - company_name: 公司名称（必填）
        - depth: 研究深度 - basic/standard/deep（必填）
        - api_key: API密钥（必填，或通过环境变量配置）

    Returns:
        任务ID和状态信息
    """
    # 输入验证
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "请求体不能为空，需为JSON格式"}), 400

    company_name = (data.get("company_name") or "").strip()
    depth = (data.get("depth") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    # 如果请求中没有 api_key，尝试从环境变量获取
    if not api_key:
        api_key = os.environ.get("SENSENOVA_API_KEY", "")

    # 验证
    if not company_name:
        return jsonify({"error": "公司名称不能为空"}), 400
    if len(company_name) > 100:
        return jsonify({"error": "公司名称过长（最大100字符）"}), 400
    if not api_key:
        return jsonify({"error": "API Key不能为空，请在页面中输入或配置环境变量 SENSENOVA_API_KEY"}), 400
    if depth not in ("basic", "standard", "deep"):
        return jsonify({"error": "深度参数必须是 basic、standard 或 deep"}), 400

    try:
        task_id = create_task(company_name, depth, api_key)

        logger.info(f"Research task created: {task_id}, company={company_name}, depth={depth}")

        return jsonify({
            "success": True,
            "task_id": task_id,
            "company_name": company_name,
            "depth": depth,
            "status": "running",
            "message": "研究任务已启动",
        }), 201

    except Exception as e:
        logger.error(f"Failed to create research task: {e}", exc_info=True)
        return jsonify({"error": f"创建任务失败: {str(e)}"}), 500


@add_route("/research/<task_id>/stream", methods=["GET"])
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


@add_route("/research/<task_id>/result", methods=["GET"])
def research_result(task_id: str):
    """
    获取研究结果（推荐用于轮询）

    Args:
        task_id: 任务ID

    Returns:
        任务状态和结果
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    result = task.get("result")
    events_count = len(task.get("events", []))

    # 构建安全的结果返回（不包含敏感信息）
    safe_result = None
    if result:
        safe_result = {
            "report": result.get("report", ""),
            "summary": result.get("summary", ""),
            "key_metrics": result.get("key_metrics", []),
            "logs": result.get("logs", []),
            "total_duration_ms": result.get("total_duration_ms", 0),
            "plan": result.get("plan", {}),
            "research_results": result.get("research_results", []),
            "review": result.get("review", {}),
            "fact_check": result.get("fact_check", {}),
            "tool_stats": result.get("tool_stats", {}),
            "api_call_count": result.get("api_call_count", 0),
            "error": result.get("error"),
        }

    return jsonify({
        "success": True,
        "task_id": task_id,
        "company_name": task["company_name"],
        "depth": task["depth"],
        "status": task["status"],
        "created_at": task["created_at"],
        "started_at": task["started_at"],
        "completed_at": task["completed_at"],
        "events_count": events_count,
        "result": safe_result,
    })


@add_route("/tasks", methods=["GET"])
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
                "created_at": task["created_at"],
                "completed_at": task["completed_at"],
                "events_count": len(task.get("events", [])),
            })

    return jsonify({
        "success": True,
        "total": len(task_list),
        "tasks": task_list,
    })


# ============================================================
# 错误处理
# ============================================================

@app.errorhandler(404)
def not_found(error):
    """404错误处理"""
    return jsonify({"error": "端点不存在"}), 404


@app.errorhandler(405)
def method_not_allowed(error):
    """405错误处理"""
    return jsonify({"error": "请求方法不允许"}), 405


@app.errorhandler(500)
def internal_error(error):
    """500错误处理"""
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "服务器内部错误"}), 500


# ============================================================
# 本地开发入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("多Agent深度研究系统 v2.0 - Vercel Serverless Function")
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
    print("  basic    - 3维度, 审核不修改, 简化事实核查 - 约90秒")
    print("  standard - 5维度, 审核+修改1轮, LLM事实核查 - 约3分钟")
    print("  deep     - 7维度, 审核+修改2轮, 全量事实核查 - 约5分钟")
    print("=" * 60)
    print("提示：")
    print("  - 生产环境部署到 Vercel 后，通过 /api/ 路径访问")
    print("  - 本地开发运行：python api/index.py")
    print("  - 前端页面：index.html (根目录)")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
