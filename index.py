"""
多Agent深度研究系统 - Vercel Serverless Function 入口
4个API端点：
- POST /api/research - 启动研究
- GET /api/research/<id>/stream - SSE实时流
- GET /api/research/<id>/result - 获取结果
- GET /api/health - 健康检查

注意：
- Vercel Serverless Function 使用 WSGI handler 运行 Flask 应用
- 此文件需要导出 `app` 或 `application` 变量
- 免费版函数最大执行时间为 10 秒，Pro版为 300 秒
- SSE 在 Serverless 环境下有超时限制，建议使用轮询方式作为降级
"""

import json
import os
import sys
import time
import uuid
import threading
import queue
from datetime import datetime

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

# 确保同目录下的模块可以被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_system import ResearchOrchestrator

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Vercel 需要的 WSGI application 变量
application = app

# 全局任务存储（内存字典）
# 注意：Serverless 环境下每次请求可能是不同实例，内存存储不可靠
# 生产环境建议使用外部存储（如 Redis、Vercel KV 等）
tasks = {}
# 任务锁
tasks_lock = threading.Lock()


def generate_task_id():
    return uuid.uuid4().hex[:16]


def create_task(company_name, depth, api_key):
    """创建新任务并启动研究"""
    task_id = generate_task_id()
    event_queue = queue.Queue()
    task = {
        "id": task_id,
        "company_name": company_name,
        "depth": depth,
        "status": "running",  # running / completed / error
        "result": None,
        "event_queue": event_queue,
        "events": [],  # 所有事件记录
        "created_at": datetime.now().isoformat(),
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
    }

    with tasks_lock:
        tasks[task_id] = task

    # 后台线程执行研究
    def event_callback(event_type, data):
        event = {"type": event_type, "data": data, "timestamp": datetime.now().isoformat()}
        event_queue.put(event)
        # 同时保存到事件列表
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["events"].append(event)

    def run_research():
        try:
            orchestrator = ResearchOrchestrator()
            result = orchestrator.run(
                company_name=company_name,
                depth=depth,
                api_key=api_key,
                event_callback=event_callback,
            )
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]["result"] = result
                    tasks[task_id]["status"] = "error" if result.get("error") else "completed"
                    tasks[task_id]["completed_at"] = datetime.now().isoformat()
        except Exception as e:
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
            event_queue.put({"type": "__end__", "data": {}, "timestamp": datetime.now().isoformat()})

    thread = threading.Thread(target=run_research, daemon=True)
    thread.start()

    return task_id


# ============================================================
# API 端点
# ============================================================

@app.route("/api/health", methods=["GET"])
def health_check():
    """健康检查"""
    with tasks_lock:
        task_count = len(tasks)
        running_count = sum(1 for t in tasks.values() if t["status"] == "running")
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "total_tasks": task_count,
        "running_tasks": running_count,
        "deployment": "vercel-serverless",
    })


@app.route("/api/research", methods=["POST"])
def start_research():
    """启动研究任务"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请求体不能为空"}), 400

    company_name = data.get("company_name", "").strip()
    depth = data.get("depth", "basic").strip()
    api_key = data.get("api_key", "").strip()

    # 如果请求中没有 api_key，尝试从环境变量获取
    if not api_key:
        api_key = os.environ.get("SENSENOVA_API_KEY", "")

    if not company_name:
        return jsonify({"error": "公司名称不能为空"}), 400
    if not api_key:
        return jsonify({"error": "API Key不能为空，请在页面中输入或配置环境变量 SENSENOVA_API_KEY"}), 400
    if depth not in ("basic", "standard", "deep"):
        return jsonify({"error": "深度参数必须是 basic、standard 或 deep"}), 400

    task_id = create_task(company_name, depth, api_key)

    return jsonify({
        "task_id": task_id,
        "company_name": company_name,
        "depth": depth,
        "status": "running",
        "message": "研究任务已启动",
    }), 201


@app.route("/api/research/<task_id>/stream", methods=["GET"])
def research_stream(task_id):
    """SSE实时流

    注意：Vercel Serverless Function 有执行时间限制
    - 免费版：10秒
    - Pro版：300秒
    研究任务可能超过这个时间，SSE连接会被中断。
    前端应实现重连机制，或使用 /result 端点轮询。
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    def generate():
        # 先发送已有事件
        with tasks_lock:
            past_events = list(task["events"])

        for event in past_events:
            if event["type"] == "__end__":
                continue
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        # 如果任务已完成，直接发送complete事件并结束
        if task["status"] in ("completed", "error"):
            # 如果最后一个事件不是complete，补一个
            if not past_events or past_events[-1]["type"] not in ("complete", "error"):
                if task["result"]:
                    yield f"data: {json.dumps({'type': 'complete', 'data': {'report': task['result'].get('report', ''), 'logs': task['result'].get('logs', []), 'total_duration_ms': task['result'].get('total_duration_ms', 0)}, 'timestamp': datetime.now().isoformat()}, ensure_ascii=False)}\n\n"
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
                # 发送心跳
                yield f"data: {json.dumps({'type': 'ping', 'data': {}, 'timestamp': datetime.now().isoformat()}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/research/<task_id>/result", methods=["GET"])
def research_result(task_id):
    """获取研究结果（推荐用于轮询）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({"error": "任务不存在"}), 404

    result = task.get("result")
    return jsonify({
        "task_id": task_id,
        "company_name": task["company_name"],
        "depth": task["depth"],
        "status": task["status"],
        "created_at": task["created_at"],
        "started_at": task["started_at"],
        "completed_at": task["completed_at"],
        "events_count": len(task.get("events", [])),
        "result": {
            "report": result.get("report", "") if result else "",
            "logs": result.get("logs", []) if result else [],
            "total_duration_ms": result.get("total_duration_ms", 0) if result else 0,
            "plan": result.get("plan", {}) if result else {},
            "error": result.get("error") if result else None,
        } if result else None,
    })


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    """列出所有任务（调试用）"""
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
            })
    return jsonify({"tasks": task_list})


# ============================================================
# 本地开发入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("多Agent深度研究系统 - Vercel Serverless Function")
    print("=" * 60)
    print("API端点：")
    print("  POST   /api/research           - 启动研究")
    print("  GET    /api/research/<id>/stream - SSE实时流")
    print("  GET    /api/research/<id>/result - 获取结果")
    print("  GET    /api/health             - 健康检查")
    print("  GET    /api/tasks              - 任务列表")
    print("=" * 60)
    print("提示：")
    print("  - 生产环境部署到 Vercel 后，通过 /api/ 路径访问")
    print("  - 本地开发运行：python api/index.py")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
