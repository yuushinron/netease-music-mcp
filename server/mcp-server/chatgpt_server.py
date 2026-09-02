#!/usr/bin/env python3
"""ChatGPT-compatible wrapper for the NetEase Music MCP server."""

import json
import logging

import server as core

logger = logging.getLogger("mcp-netease-chatgpt")


class ChatGPTMCPHandler(core.MCPHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/mcp':
            self.send_response(405)
            self._cors()
            self.send_header('Allow', 'POST, OPTIONS')
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path != '/mcp':
            self._json_response({"error": "Not found"}, 404)
            return

        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._json_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }, 400)
            return

        if isinstance(body, list):
            responses = []
            for item in body:
                response = self._handle_message(item)
                if response is not None:
                    responses.append(response)
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
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }

        method = body.get('method', '')
        req_id = body.get('id')

        if method.startswith('notifications/') or req_id is None:
            return None

        if method == 'initialize':
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "netease-music-mcp", "version": "3.1.0"},
            }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        if method == 'tools/list':
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": core.TOOLS}}

        if method == 'tools/call':
            tool_name = body.get('params', {}).get('name', '')
            arguments = body.get('params', {}).get('arguments', {})
            logger.info("Tool call: %s", tool_name)
            handler = core.TOOL_DISPATCH.get(tool_name)
            if not handler:
                result = {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                }
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            try:
                tool_result = handler(arguments)
                result = {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(tool_result, ensure_ascii=False),
                    }]
                }
            except Exception as exc:
                logger.exception("Tool error [%s]", tool_name)
                result = {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"error": str(exc)}, ensure_ascii=False),
                    }],
                    "isError": True,
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }


if __name__ == '__main__':
    logger.info(
        "Starting ChatGPT-compatible NetEase Music MCP Server v3.1.0 with %s tools on port %s",
        len(core.TOOLS),
        core.PORT,
    )
    httpd = core.ThreadedHTTPServer(('0.0.0.0', core.PORT), ChatGPTMCPHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down")
        httpd.shutdown()
