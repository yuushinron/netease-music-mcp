# Xiaohongshu Link MCP

A small ChatGPT-compatible MCP server for reading public Xiaohongshu/RED note links.

## What it does

- Accepts `xiaohongshu.com` and `xhslink.com` note URLs.
- Follows Xiaohongshu short-link redirects.
- Fetches the public mobile page with an iPhone Safari user agent.
- Parses `window.__INITIAL_STATE__`.
- Returns title, author, description, interaction counts, first-screen comments, and image URLs.
- Optionally downloads note images and returns them as MCP `image` content blocks so multimodal clients can inspect them.
- Does not require a Xiaohongshu login or cookie.

## Railway

Root Directory:

```text
/xhs-mcp
```

Start Command:

```text
python3 server.py
```

Environment:

```text
MCP_PORT=8080
```

Optional:

```text
LOG_LEVEL=INFO
XHS_MAX_IMAGE_BYTES=6291456
```

Generate a public domain for port `8080`, then use:

```text
https://YOUR-DOMAIN/mcp
```

as the custom MCP URL in ChatGPT with no authentication.

Health check:

```text
GET /health
```

For ChatGPT-compatible Streamable HTTP behavior, `GET /mcp` returns `405` and JSON-RPC notifications return `202`.

## Tool

`read_xhs_note`

Arguments:

- `url` (required)
- `max_comments` (default 10, max 30)
- `include_images` (default true)
- `max_images` (default 6, max 9)
