"""LLM agentic loop：基于 SenseNova（OpenAI 兼容）的 function-calling 智能体循环。

每个「角色派发」= 一次完整的 agent 运行：
  system = 角色 prompt（agents/<role>.md）
  user   = controller 按 SKILL.md §5 payload 契约组装的任务消息
  循环：模型思考 → 调工具 → 喂回结果 → 直到模型不再调工具（产出最终答复）

角色自己用 web_search/web_fetch 取证、用 write_file 落盘、用 run_command 跑 validator
并自纠错——这正是 sn-deep-research「evidence 为唯一真相来源」的设计。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from openai import OpenAI

from sn_agent import tools

# ── 配置 ──────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("SENSENOVA_API_KEY", "sk-ZwsINEMXRdXlFLVji9kpNkVDLq8675mJ")
BASE_URL = os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1")
MODEL = os.environ.get("SENSENOVA_MODEL", "sensenova-6.7-flash-lite")

# sensenova-6.7-flash-lite 默认带思考模式，思考会占用 completion tokens，
# 因此输出预算要给足，否则会在思考阶段就触发 length 截断（content=None）。
DEFAULT_MAX_TOKENS = 16000

# 动态客户端（由 configure() 设置）；默认用环境变量初始化
_client: Optional[OpenAI] = OpenAI(api_key=API_KEY, base_url=BASE_URL)
_current_model = MODEL
_current_api_key = API_KEY


def configure(api_key: str, model: str = "sensenova-6.7-flash-lite",
              base_url: str = "https://token.sensenova.cn/v1") -> None:
    """动态配置 API 客户端（web 端每次任务时调用，支持用户自定义 key/model）。"""
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
    # 排理思过程（reasoning）不回传给模型，避免历史膨胀 / 重复思考
    d.pop("reasoning", None)
    # SenseNova API 要求 assistant 消息含 tool_calls 时 content 字段必须存在
    # （即使为 null）。exclude_none=True 会移除 None 的 content，导致服务端
    # prompt builder 报 "Can only get item pairs from a mapping"。
    if "tool_calls" in d and "content" not in d:
        d["content"] = None
    return d


# tool_call arguments 在历史中超过此阈值则截断（避免 write_file 大内容
# 触发服务端 prompt 构建错误 / 上下文膨胀）。
_MAX_TOOL_ARG_CHARS = 6000

import re as _re


def _repair_json_args(args_str: str) -> dict | None:
    """从被截断的 JSON 字符串中尽力提取已知字段（path/content 等）。

    当模型在生成大型 tool_call arguments（如 write_file 写 29KB outline.json）
    时因 max_tokens 不足被截断，arguments 不是合法 JSON。本函数用正则提取
    顶层 string 字段，返回一个可用 dict；无法提取时返回 None。
    """
    if not args_str:
        return None
    # 先试严格解析
    try:
        v = json.loads(args_str)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    # 容错：逐个提取 "key": "value" 对（value 可含转义引号）
    result: dict = {}
    # 找 path（通常在开头，完整）
    for key in ("path",):
        m = _re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % _re.escape(key), args_str)
        if m:
            try:
                result[key] = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                result[key] = m.group(1)
    # 找 content（可能被截断 —— 取到最后一个引号之前的内容）
    m = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', args_str, _re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            result["content"] = json.loads('"' + raw + '"')
        except json.JSONDecodeError:
            # 末尾可能不完整，直接用原始文本
            result["content"] = raw + "\n[... 模型输出被截断，内容可能不完整 ...]"
    return result if result else None


def _truncate_tool_call_args(assistant_dict: dict) -> None:
    """就地截断/修复 assistant 消息中 tool_calls 的 arguments。

    两类处理：
    1. arguments 是合法 JSON 但过大 → 截断大字段后重新序列化
    2. arguments 不是合法 JSON（模型输出被截断）→ 用 _repair_json_args
       提取 path/content，替换为合法的最小 JSON。

    核心目标：保证回传给 SenseNova API 的 arguments 永远是合法 JSON 字符串，
    否则服务端 prompt builder 报 "Can only get item pairs from a mapping"。
    """
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
        # 尝试解析 JSON
        try:
            args = json.loads(args_str)
            if isinstance(args, dict):
                need_trunc = len(args_str) > _MAX_TOOL_ARG_CHARS
                if not need_trunc:
                    continue
                # 截断大字段后重新序列化
                truncated = False
                for k, v in list(args.items()):
                    if isinstance(v, str) and len(v) > 4000:
                        args[k] = v[:4000] + f"\n[... 截断，原 {len(v)} 字符 ...]"
                        truncated = True
                if truncated:
                    fn["arguments"] = json.dumps(args, ensure_ascii=False)
                continue
            # 非 dict 的合法 JSON（array/str/num）——不处理
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        # arguments 不是合法 JSON —— 必须修复，否则下次 API 调用会 400
        repaired = _repair_json_args(args_str)
        if repaired is not None:
            # 截断超长 content
            for k, v in list(repaired.items()):
                if isinstance(v, str) and len(v) > 4000:
                    repaired[k] = v[:4000] + "\n[... 截断 ...]"
            fn["arguments"] = json.dumps(repaired, ensure_ascii=False)
        else:
            # 无法提取任何字段 —— 用最小占位 JSON 保证合法
            fn["arguments"] = json.dumps(
                {"_note": "original arguments were malformed (model output truncated)"},
                ensure_ascii=False,
            )


def run_role(
    role_name: str,
    system_prompt: str,
    payload: str,
    *,
    max_iterations: int = 28,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tools_enabled: list[str] | None = None,
    verbose: bool = True,
) -> str:
    """运行一个角色到完成。

    Args:
        role_name: 角色名（用于日志，如 scout / research / report-writer）
        system_prompt: 角色系统提示（agents/<role>.md 全文）
        payload: 任务消息（SKILL.md §5 payload 契约填好的文本）
        max_iterations: 工具调用循环上限
        max_tokens: 每次模型调用的输出 token 上限
        tools_enabled: 允许该角色使用的工具子集；None=全部

    Returns:
        角色最终答复文本（通常含产出文件路径与统计摘要）。
    """
    tool_specs = tools.TOOL_SPECS
    if tools_enabled is not None:
        names = set(tools_enabled)
        tool_specs = [t for t in tools.TOOL_SPECS if t["function"]["name"] in names]

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload},
    ]

    # emit 角色开始事件
    _emit("agent_start", {
        "agent": role_name,
        "input_summary": payload[:500],
        "input_text": f"[System Prompt]\n{system_prompt[:2000]}\n\n[User Prompt]\n{payload[:3000]}",
    })

    final_text = ""
    _search_history: dict[str, int] = {}  # query → 调用次数（用于检测重复搜索循环）
    for i in range(1, max_iterations + 1):
        msg = _call_model(role_name, messages, tool_specs, max_tokens, attempt=1)
        if msg is None:
            # 重试也失败，提前结束
            break

        # 空内容 + 无工具调用 → thinking 模式吃光 token 的瞬时失败，重试一次
        if not msg.tool_calls and not (msg.content or "").strip():
            if verbose:
                _log(role_name, f"迭代 {i}：空答复（无工具调用无内容），瞬时失败重试")
            # 不把这条空 assistant 消息塞回历史，直接重新调用
            continue

        assistant_dict = _to_dict(msg)
        messages.append(assistant_dict)

        # 没有工具调用 → 角色已产出最终答复
        if not msg.tool_calls:
            final_text = msg.content or ""
            if verbose:
                _log(role_name, f"迭代 {i} 完成（无工具调用，最终答复 {len(final_text)} 字符）")
            break

        # 执行所有工具调用并喂回结果
        if verbose:
            calls = [(tc.function.name, tc.function.arguments) for tc in msg.tool_calls]
            _log(role_name, f"迭代 {i}：调用 {len(calls)} 个工具 → {[c[0] for c in calls]}")
            for tc in msg.tool_calls:
                if tc.function.name == "write_file":
                    try:
                        _args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                        _p = _args.get("path", "?")
                        _clen = len(_args.get("content", ""))
                        _log(role_name, f"    write_file(path={_p}, content_len={_clen})")
                    except Exception:
                        _log(role_name, f"    write_file(args_parse_failed, len={len(tc.function.arguments or '')})")

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                # 模型输出被截断，arguments 不是合法 JSON —— 尽力修复
                args = _repair_json_args(tc.function.arguments or "") or {}
                if verbose and args:
                    _log(role_name, f"    ⚠ {name} arguments 被截断，已修复提取 {list(args.keys())}")

            # 检测重复搜索循环：记录 web_search query，若同一 query 被调用 3+ 次，
            # 在工具结果中追加收敛提示，引导 agent 转入写 evidence 阶段
            search_key = ""
            if name == "web_search":
                search_key = args.get("query", "").strip().lower()
            elif name == "web_fetch":
                search_key = args.get("url", "").strip().lower()
            if search_key:
                _search_history[search_key] = _search_history.get(search_key, 0) + 1

            result = tools.execute_tool(name, args)

            # 对重复搜索注入收敛提示，防止 agent 陷入搜索循环
            if search_key and _search_history[search_key] >= 3:
                result += (
                    "\n\n⚠ 系统提示：该查询已被调用 "
                    f"{_search_history[search_key]} 次，结果已饱和。"
                    "请停止重复搜索相同关键词，立即用已收集的信息写入 evidence.json "
                    "（使用 write_file 工具）。如确需补充信息，请使用不同的搜索词。"
                )
                if verbose:
                    _log(role_name, f"    ⚠ 检测到重复查询（第 {_search_history[search_key]} 次），已注入收敛提示")

            # 工具结果太长则截断喂回，避免上下文爆炸
            if len(result) > 24000:
                result = result[:24000] + "\n[... 工具结果已截断 ...]"
            if verbose and name in ("web_search", "web_fetch"):
                _log(role_name, f"    {name}({args.get('query') or args.get('url','')}) → {len(result)} 字符")

            # emit 工具调用事件（供 web 端展示）
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

        # 截断已执行的 tool_call arguments（避免 write_file 大内容触发
        # 服务端 "Can only get item pairs from a mapping" 错误 / 上下文膨胀）
        _truncate_tool_call_args(messages[-1 - len(msg.tool_calls)])
        if verbose:
            arg_sizes = [
                len(tc.function.arguments or "")
                for tc in msg.tool_calls
            ]
            big = [s for s in arg_sizes if s > _MAX_TOOL_ARG_CHARS]
            if big:
                _log(role_name, f"    （已截断 {len(big)} 个过大 tool_call arguments）")

        # 收敛推进：如果已搜索很多轮但仍未产出文件，注入 user 消息引导收敛
        total_searches = sum(_search_history.values())
        unique_searches = len(_search_history)
        if i >= 20 and total_searches > 25 and unique_searches < total_searches * 0.6:
            # 重复率 > 40%，说明 agent 在循环搜索
            nudge = (
                "⚠ 系统收敛提示：你已执行多轮搜索，且存在大量重复查询。"
                "请立即停止搜索，用已收集到的信息按 schema 写入 evidence.json。"
                "如某 key_question 信息不足，在 evidence 中如实标注信息缺口，"
                "不要为了补齐缺口而无限重复搜索。"
            )
            messages.append({"role": "user", "content": nudge})
            if verbose:
                _log(role_name, f"    → 注入收敛 user 消息（{total_searches} 次搜索，{unique_searches} 个唯一查询）")
    else:
        final_text = final_text or "（已达最大迭代次数，流程终止）"
        if verbose:
            _log(role_name, f"⚠ 达到最大迭代 {max_iterations}，强制结束")

    # emit 角色结束事件
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
            import json as _json
            debug_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                f"debug_400_{role_name.replace('/','_')}.json",
            )
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    # 序列化消息，截断过长的 content
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
                    _json.dump({
                        "role": role_name,
                        "error": msg[:1000],
                        "num_messages": len(messages),
                        "total_size_chars": sum(len(_json.dumps(m, ensure_ascii=False)) for m in messages),
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
    """读取角色 prompt 文件（agents/<role>.md）。"""
    with open(role_path, "r", encoding="utf-8") as f:
        return f.read()
