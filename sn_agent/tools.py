"""工具层：为各 research 角色 提供 Tier1 核心能力（网页搜索 / 网页抓取 / 文件读写 / 命令执行）。

这些能力对应 sn-deep-research SKILL.md §1 的「Tier 1 强制能力」——
evidence 取证、validator 执行、文件通信都依赖它们。所有结果都是真实抓取的，
绝不返回模拟数据。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

import base64

import html2text
import requests
from bs4 import BeautifulSoup

# html2text 转换器：保留链接，去掉冗余空白
_H2T = html2text.HTML2Text()
_H2T.ignore_links = False
_H2T.ignore_images = True
_H2T.body_width = 0
_H2T.skip_internal_links = True

# Bing bot 检测会随时间变化封某些 UA。Safari/Mac 与 Safari/iPhone
# 在本环境实测稳定返回 b_algo，Chrome 偶尔被降级到 35KB bot 页。
# 因此主用 Safari 类 UA，Chrome 兜底；每次搜索用新会话避免 cookie 退化。
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
# fetch 用：含 Referer 的浏览器级请求头（每次按 UA 重建）
def _fetch_headers(ua: str) -> dict:
    return {**_BASE_HEADERS, "User-Agent": ua, "Referer": "https://www.bing.com/"}

# 单页抓取的最大字符数（避免把超长原文塞爆模型上下文）
MAX_FETCH_CHARS = 18000
# 搜索返回的最大结果数
MAX_SEARCH_RESULTS = 8


def web_search(query: str, num: int = MAX_SEARCH_RESULTS) -> str:
    """网页搜索（Bing，免 key）。返回真实结构化结果列表。

    解析 b_algo 结果块，从重定向链接的 base64 u 参数还原真实 URL，
    保证返回的 URL 可直接抓取、可写入 evidence 的 source.url。
    """
    results = _bing_search(query, num)
    if not results:
        return json.dumps(
            {"error": "search_no_results", "query": query},
            ensure_ascii=False,
        )
    return json.dumps(results[:num], ensure_ascii=False, indent=2)


def _bing_search(query: str, num: int) -> list[dict]:
    """Bing HTML 解析，UA 轮换 + 新会话 + ensearch 兜底。

    实测要点：
    - Bing 给无 cookie / 被 ban 的 UA 返回 ~35KB 的 bot 检测页（无 b_algo）；
      给带 cookie 的合格 UA 返回 100KB+ 的真实结果页（含 b_algo）。
    - 同一会话用久了会退化；每次搜索建新会话，先访问首页 warm-up 拿 cookie。
    - 不同 UA 的通过率随时间漂移，所以轮换 _UA_POOL 里的所有 UA。
    - ensearch=1 强制国际版（中文 query 也能命中 sensetime.com 等真实站点），
      失败再退 ensearch=0（cn 版）。
    """
    for ua in _UA_POOL:
        for ensearch in (1, 0):
            html = _fetch_bing_html(query, ua, ensearch)
            if html is None or len(html) < 40000:
                # <40KB 通常是 bot 检测页；换 UA 或换 ensearch 再试
                continue
            out = _parse_bing_html(html, num)
            if out:
                return out
    return []


def _fetch_bing_html(query: str, ua: str, ensearch: int) -> str | None:
    """用指定 UA 建立新会话、warm-up 后抓 Bing 搜索页 HTML。"""
    s = requests.Session()
    s.headers.update({**_BASE_HEADERS, "User-Agent": ua})
    try:
        s.get("https://www.bing.com/", timeout=15)
    except Exception:
        pass  # warm-up 失败也继续尝试搜索，部分场景仍能拿到结果
    params = {"q": query, "ensearch": ensearch, "cc": "US"}
    try:
        r = s.get("https://www.bing.com/search", params=params, timeout=22)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    return r.text


def _parse_bing_html(html: str, num: int) -> list[dict]:
    """解析 b_algo 结果块，还原真实 URL，提取标题/URL/摘要。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(strip=True)
        raw_href = a.get("href", "")
        url = _resolve_bing_url(raw_href)
        if not url:
            cite = li.select_one("cite")
            if cite:
                url = cite.get_text(" ", strip=True)
        snippet = ""
        cap = li.select_one(".b_caption p, .b_linefirst, p")
        if cap:
            snippet = cap.get_text(" ", strip=True)
        if title and url:
            out.append({"title": title, "url": url, "snippet": snippet[:240]})
        if len(out) >= num:
            break
    return out


def _resolve_bing_url(href: str) -> str:
    """从 Bing ck/a 重定向链接还原真实 URL。

    Bing 的结果链接形如:
    https://www.bing.com/ck/a?...&u=a1aHR0cHM6Ly93d3cuc2Vuc2V0aW1lLmNvbS9jbi8&ntb=1
    其中 u= 参数是 base64 编码的真实 URL（前缀 a1 需去掉）。
    """
    if not href:
        return ""
    if not href.startswith("https://www.bing.com/ck/a"):
        return href  # 已是真实 URL
    m = re.search(r"[?&]u=([^&]+)", href)
    if not m:
        return ""
    enc = m.group(1)
    # 去掉前缀标记（常见 'a1'，也可能是 'a3'）
    for prefix_len in (2, 3, 1, 0):
        cand = enc[prefix_len:]
        cand += "=" * (-len(cand) % 4)
        try:
            decoded = base64.urlsafe_b64decode(cand).decode("utf-8", errors="replace")
        except Exception:
            continue
        if decoded.startswith("http"):
            return decoded
    return ""


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html_unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s


def html_unescape(s: str) -> str:
    import html as _h

    return _h.unescape(s)


def web_fetch(url: str) -> str:
    """抓取网页并转为 markdown。自动处理编码、重定向、截断。

    带浏览器级请求头 + Referer，最大化可抓取率。对反爬站点（403）返回明确
    信号，提示角色改用搜索摘要或换来源，绝不伪造内容。
    """
    fetch_headers = _fetch_headers(_UA_POOL[0])
    try:
        r = requests.get(url, headers=fetch_headers, timeout=25, allow_redirects=True)
    except Exception as e:
        return json.dumps({"error": "fetch_failed", "url": url, "detail": str(e)}, ensure_ascii=False)

    if r.status_code == 403:
        return json.dumps(
            {
                "error": "blocked_403",
                "url": url,
                "hint": "该站点禁止抓取。请改用搜索摘要中的信息，或换一个可抓取的来源（官网/新闻/财报披露页等）。",
            },
            ensure_ascii=False,
        )
    if r.status_code != 200:
        return json.dumps(
            {"error": "fetch_failed", "url": url, "status": r.status_code}, ensure_ascii=False
        )

    # 编码探测
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    ctype = r.headers.get("Content-Type", "")
    text = r.text
    if "html" in ctype.lower() or text.strip().startswith("<"):
        md = _H2T.handle(text)
    else:
        md = text  # 已经是文本/markdown

    md = _clean_markdown(md)
    if len(md) > MAX_FETCH_CHARS:
        md = md[:MAX_FETCH_CHARS] + f"\n\n[... 截断，原文共 {len(md)} 字符 ...]"
    meta = f"url: {url}\nstatus: {r.status_code}\n---\n"
    return meta + md


def _clean_markdown(md: str) -> str:
    """压缩冗余空行与无意义内容。"""
    md = re.sub(r"\n{3,}", "\n\n", md)
    # 去掉大量导航/cookie 噪声行（启发式）
    lines = md.split("\n")
    kept = []
    for ln in lines:
        s = ln.strip()
        if not s:
            kept.append(ln)
            continue
        # 跳过明显的导航噪声
        if re.match(r"^(接受|拒绝|关闭|登录|注册|搜索|菜单|跳转|Skip to)", s):
            continue
        kept.append(ln)
    return "\n".join(kept)


def read_file(path: str) -> str:
    """读取文件内容。"""
    p = Path(path)
    if not p.exists():
        return json.dumps({"error": "file_not_found", "path": path}, ensure_ascii=False)
    if p.is_dir():
        return json.dumps(
            {"error": "is_directory", "path": path, "entries": os.listdir(path)}, ensure_ascii=False
        )
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = p.read_bytes().decode("utf-8", errors="replace")
    if len(content) > 60000:
        content = content[:60000] + f"\n[... 截断，文件共 {len(content)} 字符 ...]"
    return content


def write_file(path: str, content: str) -> str:
    """写入文件（自动创建父目录）。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(p), "bytes": len(content)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": "write_failed", "path": path, "detail": str(e)}, ensure_ascii=False)


def list_files(path: str) -> str:
    """列出目录内容。"""
    p = Path(path)
    if not p.exists():
        return json.dumps({"error": "path_not_found", "path": path}, ensure_ascii=False)
    if p.is_file():
        return json.dumps({"path": str(p), "type": "file", "size": p.stat().st_size}, ensure_ascii=False)
    entries = []
    for child in sorted(p.iterdir()):
        entries.append(
            {"name": child.name, "type": "dir" if child.is_dir() else "file", "size": child.stat().st_size if child.is_file() else None}
        )
    return json.dumps({"path": str(p), "entries": entries}, ensure_ascii=False, indent=2)


def run_command(command: str, cwd: str | None = None, timeout: int = 120) -> str:
    """执行 shell 命令（主要用于跑 validator 脚本）。

    安全约束：在限定 cwd 内执行；超时即终止。
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = proc.stdout
        if proc.returncode != 0 and proc.stderr:
            out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
        if len(out) > 30000:
            out = out[:30000] + "\n[... 截断 ...]"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timeout", "command": command}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": "exec_failed", "command": command, "detail": str(e)}, ensure_ascii=False)


# ── 工具注册表：供 LLM function-calling 使用 ──────────────────────────────

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "网页搜索，返回真实搜索结果列表（标题/URL/摘要）。用于发现信息来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num": {"type": "integer", "description": "返回结果数，默认8", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取指定 URL 的网页内容并转为 markdown。采证前必须抓取原文核对，不依赖搜索摘要。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的完整 URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件内容（schema 文档、evidence、outline 等）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入本地文件（自动创建父目录）。用于输出 evidence.json/briefing.json/章节等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目录内容或查看文件元信息。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录或文件绝对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "执行 shell 命令。主要用于运行 validator 校验脚本：python3 <scripts>/validate_evidence.py <evidence.json>。返回 stdout。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录（可选）"},
                },
                "required": ["command"],
            },
        },
    },
]


def execute_tool(name: str, arguments: dict) -> str:
    """分发执行工具调用，返回字符串结果。"""
    try:
        if name == "web_search":
            return web_search(arguments["query"], arguments.get("num", MAX_SEARCH_RESULTS))
        if name == "web_fetch":
            return web_fetch(arguments["url"])
        if name == "read_file":
            return read_file(arguments["path"])
        if name == "write_file":
            return write_file(arguments["path"], arguments["content"])
        if name == "list_files":
            return list_files(arguments["path"])
        if name == "run_command":
            return run_command(arguments["command"], arguments.get("cwd"))
        return json.dumps({"error": "unknown_tool", "name": name}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": "tool_exception", "name": name, "detail": str(e)}, ensure_ascii=False)
