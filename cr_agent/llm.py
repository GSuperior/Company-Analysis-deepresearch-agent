"""LLM agentic loop：cr_agent 的 function-calling 智能体循环。

设计简化点（vs sn_agent/llm.py）：
- 无 evidence.json 大文件修复逻辑（cr_agent 用 JSONL 增量卡片，单次 <1KB）
- 预算由 tools.Budget 管理，llm 只负责调工具 + emit 事件
- 保留 _truncate_tool_call_args 防止 write_section 大内容触发 400
- 保留重复搜索检测，防止 researcher 陷入循环

每个角色运行：
  system = 角色 prompt（prompts/<role>.md）
  user   = controller 组装的任务消息
  循环：模型思考 → 调工具（受预算约束）→ 喂回结果 → 直到产出最终答复
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from openai import OpenAI

from cr_agent import tools

# ── 配置 ──────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("SENSENOVA_API_KEY", "")
BASE_URL = os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1")
MODEL = os.environ.get("SENSENOVA_MODEL", "sensenova-6.7-flash-lite")

# sensenova-6.7-flash-lite 默认带思考模式，思考占用 completion tokens，
# 输出预算要给足，否则思考阶段就触发 length 截断（content=None）。
DEFAULT_MAX_TOKENS = 16000

# 动态客户端（由 configure() 设置）
_client: Optional[OpenAI] = None
_current_model = MODEL
_current_api_key = API_KEY


def configure(api_key: str, model: str = "sensenova-6.7-flash-lite",
              base_url: str = "https://token.sensenova.cn/v1") -> None:
    """动态配置 API 客户端（web 端每次任务时调用）。"""
    global _client, _current_model, _current_api_key
    _client = OpenAI(api_key=api_key, base_url=base_url)
    _current_model = model
    _current_api_key = api_key


# ── 事件回调（供 web 端订阅 agent 运行过程） ──────────────────────────────
_emit_callback: Optional[Callable[[str, dict], None]] = None


def set_emit_callback(cb: Optional[Callable[[str, dict], None]]) -> None:
    global _emit_callback
    _emit_callback = cb


def _emit(event_type: str, data: dict) -> None:
    if _emit_callback:
        try:
            _emit_callback(event_type, data)
        except Exception:
            pass


def _log(stage: str, msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {stage} | {msg}", flush=True)
    except (BrokenPipeError, OSError):
        pass


def _to_dict(msg: Any) -> dict:
    """把 SDK 的 ChatCompletionMessage 转成可回传历史 dict（保留 tool_calls）。"""
    d = msg.model_dump(exclude_none=True)
    # 排理思过程（reasoning）不回传给模型，避免历史膨胀
    d.pop("reasoning", None)
    # SenseNova API 要求 assistant 消息含 tool_calls 时 content 字段必须存在
    if "tool_calls" in d and "content" not in d:
        d["content"] = None
    return d


# tool_call arguments 在历史中超过此阈值则截断（避免大内容触发服务端错误）
_MAX_TOOL_ARG_CHARS = 6000

import re as _re


def _repair_json_args(args_str: str) -> dict | None:
    """从被截断的 JSON 字符串中提取已知字段（content 等）。"""
    if not args_str:
        return None
    try:
        v = json.loads(args_str)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    result: dict = {}
    for key in ("section_id", "title"):
        m = _re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % _re.escape(key), args_str)
        if m:
            try:
                result[key] = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                result[key] = m.group(1)
    m = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', args_str, _re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            result["content"] = json.loads('"' + raw + '"')
        except json.JSONDecodeError:
            result["content"] = raw + "\n[... 模型输出被截断 ...]"
    return result if result else None


def _truncate_tool_call_args(assistant_dict: dict) -> None:
    """就地截断/修复 assistant 消息中 tool_calls 的 arguments。"""
    tcs = assistant_dict.get("tool_calls")
    if not isinstance(tcs, list):
        return
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args_str = fn.get("arguments")
        if not isinstance(args_str, str):
            continue
        try:
            args = json.loads(args_str)
            if isinstance(args, dict):
                if len(args_str) <= _MAX_TOOL_ARG_CHARS:
                    continue
                truncated = False
                for k, v in list(args.items()):
                    if isinstance(v, str) and len(v) > 4000:
                        args[k] = v[:4000] + f"\n[... 截断，原 {len(v)} 字符 ...]"
                        truncated = True
                if truncated:
                    fn["arguments"] = json.dumps(args, ensure_ascii=False)
                continue
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        repaired = _repair_json_args(args_str)
        if repaired is not None:
            for k, v in list(repaired.items()):
                if isinstance(v, str) and len(v) > 4000:
                    repaired[k] = v[:4000] + "\n[... 截断 ...]"
            fn["arguments"] = json.dumps(repaired, ensure_ascii=False)
        else:
            fn["arguments"] = json.dumps(
                {"_note": "original arguments were malformed"},
                ensure_ascii=False,
            )


def run_role(
    role_name: str,
    system_prompt: str,
    payload: str,
    *,
    max_iterations: int = 24,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tools_enabled: list[str] | None = None,
    verbose: bool = True,
) -> str:
    """运行一个角色到完成。

    Args:
        role_name: 角色名（如 scout / planner / researcher / writer / reviewer）
        system_prompt: 角色系统提示
        payload: 任务消息（controller 组装的文本）
        max_iterations: 工具调用循环上限
        max_tokens: 每次模型调用的输出 token 上限
        tools_enabled: 允许该角色使用的工具子集；None=全部

    Returns:
        角色最终答复文本。
    """
    tool_specs = tools.get_tool_specs(tools_enabled)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload},
    ]

    _emit("agent_start", {
        "agent": role_name,
        "input_summary": payload[:500],
        "input_text": f"[System Prompt]\n{system_prompt[:2000]}\n\n[User Prompt]\n{payload[:3000]}",
    })

    final_text = ""
    _search_history: dict[str, int] = {}
    for i in range(1, max_iterations + 1):
        msg = _call_model(role_name, messages, tool_specs, max_tokens, attempt=1)
        if msg is None:
            break

        # 空内容 + 无工具调用 → 瞬时失败，重试
        if not msg.tool_calls and not (msg.content or "").strip():
            if verbose:
                _log(role_name, f"迭代 {i}：空答复，重试")
            continue

        assistant_dict = _to_dict(msg)
        messages.append(assistant_dict)

        # 没有工具调用 → 角色已产出最终答复
        if not msg.tool_calls:
            final_text = msg.content or ""
            if verbose:
                _log(role_name, f"迭代 {i} 完成（最终答复 {len(final_text)} 字符）")
            break

        # 执行所有工具调用
        if verbose:
            calls = [(tc.function.name, tc.function.arguments) for tc in msg.tool_calls]
            _log(role_name, f"迭代 {i}：调用 {len(calls)} 个工具 → {[c[0] for c in calls]}")

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = _repair_json_args(tc.function.arguments or "") or {}
                if verbose and args:
                    _log(role_name, f"    ⚠ {name} arguments 被截断，已修复提取 {list(args.keys())}")

            # 记录搜索查询用于重复检测
            search_key = ""
            if name == "web_search":
                search_key = args.get("query", "").strip().lower()
            elif name == "web_fetch":
                search_key = args.get("url", "").strip().lower()
            if search_key:
                _search_history[search_key] = _search_history.get(search_key, 0) + 1

            result = tools.execute_tool(name, args)

            # 对重复搜索注入收敛提示
            if search_key and _search_history[search_key] >= 3:
                result += (
                    "\n\n⚠ 系统提示：该查询已被调用 "
                    f"{_search_history[search_key]} 次，结果已饱和。"
                    "请停止重复搜索相同关键词，用已收集的信息提交卡片"
                    "（add_card）或调用 submit_research。"
                )
                if verbose:
                    _log(role_name, f"    ⚠ 重复查询（第 {_search_history[search_key]} 次），注入收敛提示")

            # 工具结果太长则截断
            if len(result) > 24000:
                result = result[:24000] + "\n[... 工具结果已截断 ...]"
            if verbose and name in ("web_search", "web_fetch"):
                _log(role_name, f"    {name}({args.get('query') or args.get('url','')}) → {len(result)} 字符")

            # emit 工具调用事件
            _emit("tool_call", {
                "agent": role_name,
                "tool": name,
                "arguments": json.dumps(args, ensure_ascii=False)[:1000],
                "result_summary": result[:500],
                "iteration": i,
            })

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )

        # 截断已执行的 tool_call arguments
        _truncate_tool_call_args(messages[-1 - len(msg.tool_calls)])

        # 收敛推进：多轮搜索 + 高重复率 → 注入 user 消息引导收敛
        total_searches = sum(_search_history.values())
        unique_searches = len(_search_history)
        if i >= 16 and total_searches > 20 and unique_searches < total_searches * 0.6:
            nudge = (
                "⚠ 系统收敛提示：你已执行多轮搜索且存在大量重复查询。"
                "请立即停止搜索，用已收集到的信息提交证据卡片（add_card）"
                "并调用 submit_research 声明完成。"
            )
            messages.append({"role": "user", "content": nudge})
            if verbose:
                _log(role_name, f"    → 注入收敛消息（{total_searches} 次搜索，{unique_searches} 唯一）")
    else:
        final_text = final_text or "（已达最大迭代次数，流程终止）"
        if verbose:
            _log(role_name, f"⚠ 达到最大迭代 {max_iterations}，强制结束")

    _emit("agent_end", {
        "agent": role_name,
        "status": "success" if final_text else "failed",
        "output_summary": final_text[:500] if final_text else "(no output)",
        "output_text": final_text[:5000],
    })

    return final_text


def _call_model(
    role_name: str,
    messages: list[dict],
    tool_specs: list[dict],
    max_tokens: int,
    attempt: int,
) -> Any:
    """调用 SenseNova，带瞬时错误重试。"""
    if _client is None:
        raise RuntimeError("LLM 客户端未配置，请先调用 configure()")
    try:
        resp = _client.chat.completions.create(
            model=_current_model,
            messages=messages,
            tools=tool_specs if tool_specs else None,
            tool_choice="auto" if tool_specs else None,
            max_tokens=max_tokens,
            temperature=0.6,
        )
        return resp.choices[0].message
    except Exception as e:
        msg = str(e)
        # 400 错误：dump 消息结构到 debug 文件，帮助诊断
        if "400" in msg or "mapping" in msg.lower():
            debug_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                f"debug_400_{role_name.replace('/','_')}.json",
            )
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    dbg_msgs = []
                    for m in messages:
                        mcopy = dict(m) if isinstance(m, dict) else {"_raw": str(m)}
                        c = mcopy.get("content")
                        if isinstance(c, str) and len(c) > 2000:
                            mcopy["content"] = c[:2000] + f"...[truncated, {len(c)} total]"
                        tcs = mcopy.get("tool_calls")
                        if isinstance(tcs, list):
                            tcs_copy = []
                            for tc in tcs:
                                tc_c = dict(tc) if isinstance(tc, dict) else {}
                                fn = tc_c.get("function") or {}
                                fn_c = dict(fn) if isinstance(fn, dict) else {}
                                args = fn_c.get("arguments", "")
                                if isinstance(args, str) and len(args) > 2000:
                                    fn_c["arguments"] = args[:2000] + f"...[truncated, {len(args)} total]"
                                tc_c["function"] = fn_c
                                tcs_copy.append(tc_c)
                            mcopy["tool_calls"] = tcs_copy
                        dbg_msgs.append(mcopy)
                    json.dump({
                        "role": role_name,
                        "error": msg[:1000],
                        "num_messages": len(messages),
                        "total_size_chars": sum(len(json.dumps(m, ensure_ascii=False)) for m in messages),
                        "message_roles": [m.get("role") for m in messages if isinstance(m, dict)],
                        "messages": dbg_msgs,
                    }, f, ensure_ascii=False, indent=2)
                _log(role_name, f"调试 dump 已写入 {debug_path}")
            except Exception as dump_err:
                _log(role_name, f"调试 dump 失败：{dump_err}")
        # 限流 / 服务端瞬时错误 → 退避重试
        if attempt < 4 and ("rate" in msg.lower() or "429" in msg or "timeout" in msg.lower()
                            or "502" in msg or "503" in msg or "504" in msg):
            wait = 15 * attempt
            _log(role_name, f"瞬时错误（{attempt}/4），{wait}s 后重试：{msg[:120]}")
            time.sleep(wait)
            return _call_model(role_name, messages, tool_specs, max_tokens, attempt + 1)
        _log(role_name, f"✗ 调用失败（不再重试）：{msg[:200]}")
        return None


def load_role_prompt(role_path: str) -> str:
    """读取角色 prompt 文件。"""
    with open(role_path, "r", encoding="utf-8") as f:
        return f.read()
