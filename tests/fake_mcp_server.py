"""测试用的假 MCP 服务端。

刻意在第一行输出一段非 JSON 噪声 —— 真实的 MCP 服务端会往 stdout 混日志，
客户端必须能容忍，否则握手会莫名其妙卡死。
"""

import json
import sys

NOISE = "this line is not json and must be ignored by the client"

TOOLS = [
    {
        "name": "get_pull_request",
        "description": "Get a pull request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pull_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
    {
        "name": "get_pull_request_files",
        "description": "List changed files with patches",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {}, "repo": {}, "pull_number": {}},
            "required": ["owner"],
        },
    },
]

PR_PAYLOAD = {
    "number": 7,
    "title": "Fix the thing",
    "html_url": "https://github.com/octocat/hello-world/pull/7",
    "head": {"sha": "deadbeef"},
}

FILES_PAYLOAD = [
    {"filename": "app/db.py", "patch": "@@ -1,1 +1,2 @@\n x\n+DB_PASSWORD = \"hunter2_super_secret\"\n"},
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdout.write(NOISE + "\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-github-mcp", "version": "0.1.0"},
                },
            })
        elif method == "notifications/initialized":
            continue  # 通知没有 id，不回复
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if name == "get_pull_request":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(PR_PAYLOAD)}]
                }})
            elif name == "get_pull_request_files":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(FILES_PAYLOAD)}]
                }})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool {name}"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
