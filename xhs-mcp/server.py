#!/usr/bin/env python3
"""Xiaohongshu link reader MCP for ChatGPT.

Pure Python / zero third-party dependencies.
Reads public Xiaohongshu note pages with a mobile browser UA, extracts
window.__INITIAL_STATE__, returns note metadata/comments, and can attach
note images as MCP image content blocks so multimodal clients can see them.
"""

import base64
import http.server
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from http.server import HTTPServer

PORT = int(os.environ.get("MCP_PORT", "8080"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_IMAGE_BYTES = int(os.environ.get("XHS_MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("xhs-mcp")

ALLOWED_PAGE_HOSTS = ("xiaohongshu.com", "xhslink.com")
ALLOWED_IMAGE_HOSTS = ("xiaohongshu.com", "xhscdn.com", "xhslink.com")


def _allowed_host(url, suffixes):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _normalize_url(url):
    if not isinstance(url, str):
        return ""
    url = url.replace("\\u002F", "/").replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def _fetch(url, referer=None, max_bytes=None):
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/json,image/avif,image/webp,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        final_url = resp.geturl()
        if max_bytes is None:
            data = resp.read()
        else:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"resource too large (>{max_bytes} bytes)")
        return final_url, resp.headers, data


def _replace_undefined_outside_strings(text):
    out = []
    i = 0
    in_string = False
    quote = ""
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text.startswith("undefined", i):
            before = text[i - 1] if i else " "
            after = text[i + 9] if i + 9 < len(text) else " "
            if not (before.isalnum() or before in "_$" or after.isalnum() or after in "_$"):
                out.append("null")
                i += 9
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_initial_state(html):
    markers = ["window.__INITIAL_STATE__", "__INITIAL_STATE__"]
    start = -1
    for marker in markers:
        pos = html.find(marker)
        if pos >= 0:
            eq = html.find("=", pos + len(marker))
            if eq >= 0:
                start = eq + 1
                break
    if start < 0:
        raise ValueError("window.__INITIAL_STATE__ not found")

    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] != "{":
        raise ValueError("__INITIAL_STATE__ does not start with an object")

    depth = 0
    in_string = False
    quote = ""
    escaped = False
    end = None
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ValueError("unterminated __INITIAL_STATE__ object")

    raw = html[start:end]
    raw = _replace_undefined_outside_strings(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot parse __INITIAL_STATE__: {exc}") from exc


def _deep_get(obj, path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _score_note_candidate(obj):
    if not isinstance(obj, dict):
        return -1
    keys = set(obj.keys())
    score = 0
    for key, weight in {
        "title": 4,
        "desc": 4,
        "imageList": 5,
        "user": 3,
        "userInfo": 3,
        "interactInfo": 3,
        "noteId": 2,
        "id": 1,
    }.items():
        if key in keys:
            score += weight
    return score


def _find_best_note(obj):
    explicit = [
        _deep_get(obj, ["noteData", "data", "noteData"]),
        _deep_get(obj, ["noteData", "normalNotePreloadData"]),
        _deep_get(obj, ["noteData", "data", "note"]),
    ]
    best = None
    best_score = -1
    stack = [x for x in explicit if x is not None] + [obj]
    seen = set()
    while stack:
        cur = stack.pop()
        oid = id(cur)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(cur, dict):
            score = _score_note_candidate(cur)
            if score > best_score:
                best, best_score = cur, score
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    if best is None or best_score < 7:
        raise ValueError("note data not found in __INITIAL_STATE__")
    return best


def _first(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def _extract_image_urls(note):
    image_list = _first(note, "imageList", "images", default=[]) or []
    urls = []
    for item in image_list:
        candidates = []
        if isinstance(item, str):
            candidates.append(item)
        elif isinstance(item, dict):
            for key in ("urlDefault", "urlPre", "url", "traceId"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith(("http", "//")):
                    candidates.append(value)
            info_list = item.get("infoList") or []
            if isinstance(info_list, list):
                for info in info_list:
                    if isinstance(info, dict):
                        value = _first(info, "url", "urlDefault", "urlPre")
                        if isinstance(value, str):
                            candidates.append(value)
        chosen = ""
        for candidate in candidates:
            candidate = _normalize_url(candidate)
            if candidate.startswith("http") and _allowed_host(candidate, ALLOWED_IMAGE_HOSTS):
                chosen = candidate
                break
        if chosen and chosen not in urls:
            urls.append(chosen)
    return urls


def _extract_comments(state, limit):
    candidates = []

    def walk(obj, under_comment=False):
        if isinstance(obj, dict):
            for key, value in obj.items():
                is_comment = under_comment or "comment" in str(key).lower()
                if is_comment and isinstance(value, list):
                    candidates.append(value)
                walk(value, is_comment)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, under_comment)

    walk(state)
    out = []
    seen = set()
    for group in candidates:
        for item in group:
            if not isinstance(item, dict):
                continue
            content = _first(item, "content", "text", "desc")
            if not isinstance(content, str) or not content.strip():
                continue
            user = _first(item, "userInfo", "user", default={}) or {}
            username = _first(user, "nickname", "nickName", "name", default="") if isinstance(user, dict) else ""
            ip = _first(item, "ipLocation", "ip_location", default="")
            key = (username, content.strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"user": username, "content": content.strip(), "ipLocation": ip})
            if len(out) >= limit:
                return out
    return out


def read_xhs_note(params):
    url = (params.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    if not _allowed_host(url, ALLOWED_PAGE_HOSTS):
        raise ValueError("only xiaohongshu.com and xhslink.com links are allowed")

    max_comments = max(0, min(int(params.get("max_comments", 10)), 30))
    include_images = bool(params.get("include_images", True))
    max_images = max(0, min(int(params.get("max_images", 6)), 9))

    final_url, headers, body = _fetch(url, max_bytes=8 * 1024 * 1024)
    if not _allowed_host(final_url, ALLOWED_PAGE_HOSTS):
        raise ValueError("redirected outside Xiaohongshu")
    charset = headers.get_content_charset() or "utf-8"
    html = body.decode(charset, errors="replace")
    state = _extract_initial_state(html)
    note = _find_best_note(state)

    user = _first(note, "user", "userInfo", default={}) or {}
    interact = _first(note, "interactInfo", "interactionInfo", default={}) or {}
    image_urls = _extract_image_urls(note)
    comments = _extract_comments(state, max_comments)

    payload = {
        "url": final_url,
        "note_id": _first(note, "noteId", "id", default=""),
        "title": _first(note, "title", default=""),
        "author": _first(user, "nickname", "nickName", "name", default="") if isinstance(user, dict) else "",
        "desc": _first(note, "desc", "description", default=""),
        "image_count": len(image_urls),
        "images": image_urls,
        "liked_count": _first(interact, "likedCount", "likeCount", default="") if isinstance(interact, dict) else "",
        "comment_count": _first(interact, "commentCount", default="") if isinstance(interact, dict) else "",
        "collected_count": _first(interact, "collectedCount", "collectCount", default="") if isinstance(interact, dict) else "",
        "comments": comments,
    }

    content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]

    image_errors = []
    if include_images:
        for index, image_url in enumerate(image_urls[:max_images], 1):
            try:
                _, image_headers, image_data = _fetch(
                    image_url,
                    referer=final_url,
                    max_bytes=MAX_IMAGE_BYTES,
                )
                mime = (image_headers.get_content_type() or "image/jpeg").lower()
                if not mime.startswith("image/"):
                    raise ValueError(f"unexpected content type: {mime}")
                content.append({
                    "type": "image",
                    "data": base64.b64encode(image_data).decode("ascii"),
                    "mimeType": mime,
                })
            except Exception as exc:
                logger.warning("Image %s failed: %s", index, exc)
                image_errors.append({"index": index, "url": image_url, "error": str(exc)})
    if image_errors:
        content.append({"type": "text", "text": json.dumps({"image_errors": image_errors}, ensure_ascii=False)})
    return content


TOOLS = [
    {
        "name": "read_xhs_note",
        "description": "Read a public Xiaohongshu/RED note from its share URL. Returns title, author, body text, interaction counts, first-screen comments, image URLs, and optionally attaches the note images so the AI can inspect them visually.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "xiaohongshu.com or xhslink.com note URL"},
                "max_comments": {"type": "integer", "description": "Maximum comments to return, 0-30 (default 10)"},
                "include_images": {"type": "boolean", "description": "Attach note images as MCP image content (default true)"},
                "max_images": {"type": "integer", "description": "Maximum images to attach, 0-9 (default 6)"},
            },
            "required": ["url"],
        },
    }
]

TOOL_DISPATCH = {"read_xhs_note": read_xhs_note}


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json_response({"status": "ok", "tools": len(TOOLS), "version": "1.0.0"})
        elif path == "/mcp":
            self.send_response(405)
            self._cors()
            self.send_header("Allow", "POST, OPTIONS")
            self.end_headers()
        else:
            self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/mcp":
            self._json_response({"error": "Not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
            return

        if isinstance(body, list):
            responses = [r for r in (self._handle_message(item) for item in body) if r is not None]
            if responses:
                self._json_response(responses)
            else:
                self.send_response(202)
                self._cors()
                self.end_headers()
            return

        response = self._handle_message(body)
        if response is None:
            self.send_response(202)
            self._cors()
            self.end_headers()
        else:
            self._json_response(response)

    def _handle_message(self, body):
        if not isinstance(body, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        method = body.get("method", "")
        req_id = body.get("id")
        if method.startswith("notifications/") or req_id is None:
            return None
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "xiaohongshu-link-mcp", "version": "1.0.0"},
            }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = body.get("params", {}) or {}
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            handler = TOOL_DISPATCH.get(tool_name)
            if not handler:
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}], "isError": True}}
            try:
                content = handler(arguments)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": content}}
            except Exception as exc:
                logger.exception("Tool error [%s]", tool_name)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    def log_message(self, fmt, *args):
        pass


class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        thread = threading.Thread(target=self._handle, args=(request, client_address), daemon=True)
        thread.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            logger.exception("Request failed")
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    logger.info("Starting Xiaohongshu Link MCP v1.0.0 on port %s", PORT)
    server = ThreadedHTTPServer(("0.0.0.0", PORT), MCPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
