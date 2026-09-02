#!/usr/bin/env python3
"""Local Xiaohongshu MCP for ChatGPT.

Runs on the user's Windows PC and uses a persistent Microsoft Edge profile via
Playwright. This keeps Xiaohongshu login state on the local machine instead of
sending cookies to Railway or GitHub.
"""

import argparse
import base64
import http.server
import json
import logging
import os
import urllib.parse
from pathlib import Path
from http.server import HTTPServer

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PORT = int(os.environ.get("MCP_PORT", "8080"))
PROFILE_DIR = Path(os.environ.get("XHS_PROFILE_DIR", str(Path(__file__).with_name("profile")))).resolve()
BROWSER_CHANNEL = os.environ.get("XHS_BROWSER_CHANNEL", "msedge")
HEADLESS = os.environ.get("XHS_HEADLESS", "0") == "1"
MAX_IMAGE_BYTES = int(os.environ.get("XHS_MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xhs-local-mcp")

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
        if key in obj:
            score += weight
    return score


def _find_best_note(state):
    explicit = [
        _deep_get(state, ["noteData", "data", "noteData"]),
        _deep_get(state, ["noteData", "normalNotePreloadData"]),
        _deep_get(state, ["noteData", "data", "note"]),
    ]
    stack = [x for x in explicit if x is not None] + [state]
    seen = set()
    best = None
    best_score = -1
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
        raise ValueError("note data not found")
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
            for key in ("urlDefault", "urlPre", "url"):
                value = item.get(key)
                if isinstance(value, str):
                    candidates.append(value)
            for info in item.get("infoList") or []:
                if isinstance(info, dict):
                    value = _first(info, "url", "urlDefault", "urlPre")
                    if isinstance(value, str):
                        candidates.append(value)
        for candidate in candidates:
            candidate = _normalize_url(candidate)
            if candidate.startswith("http") and _allowed_host(candidate, ALLOWED_IMAGE_HOSTS):
                if candidate not in urls:
                    urls.append(candidate)
                break
    return urls


def _extract_comments(state, limit):
    groups = []

    def walk(obj, under_comment=False):
        if isinstance(obj, dict):
            for key, value in obj.items():
                flag = under_comment or "comment" in str(key).lower()
                if flag and isinstance(value, list):
                    groups.append(value)
                walk(value, flag)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, under_comment)

    walk(state)
    out = []
    seen = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            text = _first(item, "content", "text", "desc")
            if not isinstance(text, str) or not text.strip():
                continue
            user = _first(item, "userInfo", "user", default={}) or {}
            name = _first(user, "nickname", "nickName", "name", default="") if isinstance(user, dict) else ""
            ip = _first(item, "ipLocation", "ip_location", default="")
            key = (name, text.strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"user": name, "content": text.strip(), "ipLocation": ip})
            if len(out) >= limit:
                return out
    return out


class BrowserSession:
    def __init__(self):
        self.pw = None
        self.context = None

    def ensure(self):
        if self.context is not None:
            return self.context
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        logger.info("Opening Microsoft Edge with profile: %s", PROFILE_DIR)
        self.context = self.pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel=BROWSER_CHANNEL,
            headless=HEADLESS,
            viewport=None,
        )
        return self.context

    def close(self):
        if self.context is not None:
            self.context.close()
            self.context = None
        if self.pw is not None:
            self.pw.stop()
            self.pw = None


BROWSER = BrowserSession()


def _security_error(page):
    url = page.url or ""
    title = ""
    body = ""
    try:
        title = page.title()
    except Exception:
        pass
    try:
        body = page.locator("body").inner_text(timeout=3000)[:3000]
    except Exception:
        pass
    haystack = (url + "\n" + title + "\n" + body).lower()
    markers = ["xhs_sec_server", "安全验证", "访问受限", "异常访问", "请完成验证", "captcha"]
    hit = next((m for m in markers if m.lower() in haystack), None)
    if hit:
        return {"type": "security_verification", "marker": hit, "url": url, "title": title}
    return None


def read_xhs_note(params):
    url = (params.get("url") or "").strip()
    if not url or not _allowed_host(url, ALLOWED_PAGE_HOSTS):
        raise ValueError("url must be a xiaohongshu.com or xhslink.com link")
    max_comments = max(0, min(int(params.get("max_comments", 10)), 30))
    max_images = max(0, min(int(params.get("max_images", 6)), 9))
    include_images = bool(params.get("include_images", True))

    context = BROWSER.ensure()
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        logger.warning("Navigation timed out; attempting to inspect loaded page")

    sec = _security_error(page)
    if sec:
        return [{"type": "text", "text": json.dumps({"ok": False, "error": sec}, ensure_ascii=False)}]

    final_url = page.url
    if not _allowed_host(final_url, ALLOWED_PAGE_HOSTS):
        raise ValueError(f"redirected outside Xiaohongshu: {final_url}")

    state = page.evaluate("() => window.__INITIAL_STATE__ || null")
    if not isinstance(state, dict):
        raise ValueError("window.__INITIAL_STATE__ unavailable; open login.bat and verify the page in Edge")

    note = _find_best_note(state)
    user = _first(note, "user", "userInfo", default={}) or {}
    interact = _first(note, "interactInfo", "interactionInfo", default={}) or {}
    image_urls = _extract_image_urls(note)
    comments = _extract_comments(state, max_comments)

    payload = {
        "ok": True,
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
    if include_images:
        for image_url in image_urls[:max_images]:
            try:
                response = context.request.get(image_url, headers={"Referer": final_url}, timeout=20000)
                if not response.ok:
                    continue
                raw = response.body()
                if len(raw) > MAX_IMAGE_BYTES:
                    continue
                mime = (response.headers.get("content-type") or "image/jpeg").split(";", 1)[0]
                if not mime.startswith("image/"):
                    continue
                content.append({"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": mime})
            except Exception as exc:
                logger.warning("Image fetch failed: %s", exc)
    return content


TOOLS = [{
    "name": "read_xhs_note",
    "description": "Read a Xiaohongshu/RED note using the user's locally logged-in Microsoft Edge profile. Returns title, author, text, interaction counts, comments, and images.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_comments": {"type": "integer", "default": 10},
            "include_images": {"type": "boolean", "default": True},
            "max_images": {"type": "integer", "default": 6},
        },
        "required": ["url"],
    },
}]


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json({"status": "ok", "mode": "local-playwright", "browser": BROWSER_CHANNEL})
        elif path == "/mcp":
            self.send_response(405)
            self._cors()
            self.send_header("Allow", "POST, OPTIONS")
            self.end_headers()
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/mcp":
            self._json({"error": "Not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
            return

        method = body.get("method", "") if isinstance(body, dict) else ""
        req_id = body.get("id") if isinstance(body, dict) else None
        if method.startswith("notifications/") or req_id is None:
            self.send_response(202)
            self._cors()
            self.end_headers()
            return

        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "xhs-local-mcp", "version": "1.0.0"}}
            self._json({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        if method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
            return
        if method == "tools/call":
            params = body.get("params", {}) or {}
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            if name != "read_xhs_note":
                result = {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
            else:
                try:
                    result = {"content": read_xhs_note(args)}
                except Exception as exc:
                    logger.exception("read_xhs_note failed")
                    result = {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)}], "isError": True}
            self._json({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        self._json({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})

    def log_message(self, format, *args):
        pass


def login_once():
    context = BROWSER.ensure()
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=35000)
    print("\nMicrosoft Edge 已打开。请在这个专用窗口里登录小红书。")
    print("确认首页能正常打开后，回到这个黑色窗口按 Enter 保存并退出。\n")
    input()
    BROWSER.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    if args.login:
        login_once()
        return
    logger.info("Starting local Xiaohongshu MCP on http://127.0.0.1:%s", PORT)
    logger.info("Persistent Edge profile: %s", PROFILE_DIR)
    server = HTTPServer(("127.0.0.1", PORT), MCPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        BROWSER.close()
        server.server_close()


if __name__ == "__main__":
    main()
