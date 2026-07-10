"""
多Agent深度研究系统 - Vercel Serverless Function 入口 (Catch-all 路由)

使用 catch-all 动态路由捕获所有 /api/* 请求，
确保 Flask 应用能正确处理所有 API 端点。

5个API端点：
- GET  /api/health - 健康检查
- POST /api/research - 启动研究任务
- GET  /api/research/<id>/stream - SSE实时流
- GET  /api/research/<id>/result - 获取研究结果
- GET  /api/tasks - 任务列表（调试用）
"""

import sys
import os

# 确保同目录下的模块可以被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 从 index.py 导入 Flask 应用
from index import app, application  # noqa: F401

# Vercel Python runtime 需要 app 或 application 变量
# 这里直接复用 index.py 中定义的 app
