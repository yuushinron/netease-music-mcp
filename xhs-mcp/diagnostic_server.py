#!/usr/bin/env python3
"""Diagnostic wrapper for the Xiaohongshu MCP server.

This keeps the existing MCP protocol/tool surface unchanged, but makes page-fetch
failures explicit so we can distinguish an HTTP error, a security/challenge page,
a login page, or an ordinary parser breakage.
"""

import html as html_lib
import logging
import re
import urllib.error

import server as core

logger = logging.getLogger("xhs-mcp-diagnostic")
_orig_fetch = core._fetch


def _page_title(text):
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return html_lib.unescape(value)[:160]


def _classify(text):
    lower = text.lower()
    signals = []
    checks = [
        ("xhs_sec_server", "xhs_sec_server"),
        ("captcha", "captcha"),
        ("安全验证", "security_verification"),
        ("验证", "verification"),
        ("访问受限", "access_restricted"),
        ("异常访问", "abnormal_access"),
        ("登录", "login_page"),
        ("risk", "risk_control"),
    ]
    for needle, label in checks:
        if needle.lower() in lower:
            signals.append(label)
    return signals


def diagnostic_fetch(url, referer=None, max_bytes=None):
    try:
        final_url, headers, data = _orig_fetch(url, referer=referer, max_bytes=max_bytes)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(512 * 1024)
            text = raw.decode(exc.headers.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            text = ""
        title = _page_title(text)
        signals = _classify(text)
        raise ValueError(
            f"XHS HTTP {exc.code}; final_url={exc.geturl()}; "
            f"title={title!r}; signals={signals or ['none']}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"XHS network error: {exc.reason}") from exc

    # Only inspect Xiaohongshu HTML pages. Image downloads still pass through.
    try:
        is_page = core._allowed_host(final_url, core.ALLOWED_PAGE_HOSTS)
        content_type = (headers.get_content_type() or "").lower()
    except Exception:
        is_page = False
        content_type = ""

    if is_page and ("html" in content_type or not content_type):
        charset = headers.get_content_charset() or "utf-8"
        text = data.decode(charset, errors="replace")
        if "__INITIAL_STATE__" not in text:
            title = _page_title(text)
            signals = _classify(text)
            raise ValueError(
                "XHS page returned without __INITIAL_STATE__; "
                f"final_url={final_url}; title={title!r}; "
                f"signals={signals or ['none']}; bytes={len(data)}"
            )

    return final_url, headers, data


core._fetch = diagnostic_fetch


if __name__ == "__main__":
    logger.info("Starting Xiaohongshu MCP diagnostic server on port %s", core.PORT)
    server = core.ThreadedHTTPServer(("0.0.0.0", core.PORT), core.MCPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down")
        server.shutdown()
