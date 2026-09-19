"""最小可用的 MCP（Model Context Protocol）stdio 客户端。

为什么自己实现：项目里要用 GitHub MCP Server 拉 PR 数据，而被审查的仓库
未必允许装第三方 SDK；MCP 的 stdio 传输协议本身很简单（换行分隔的 JSON-RPC 2.0），
自己实现一遍大约 150 行，换来的是「零依赖 + 可离线测试」。

协议要点（2024-11-05 版本）：
1. 客户端先发 `initialize` 请求，拿到服务端能力声明；
2. 再发 `notifications/initialized` 通知（**通知没有 id，服务端不回复**）；
3. 之后才能 `tools/list` 与 `tools/call`。

坑：服务端会往 stderr 打日志、甚至往 stdout 混入非 JSON 行。
读取侧必须容忍解析失败的行并跳过，否则握手会莫名其妙地卡住。
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "pr-review-agent", "version": "1.0.0"}


class MCPError(RuntimeError):
    pass


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])


class MCPStdioClient:
    """通过子进程 stdin/stdout 与 MCP 服务端通信。"""

    def __init__(
        self,
        command: str,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
        stderr_prefix: str = "[mcp]",
    ) -> None:
        if not command.strip():
            raise MCPError("MCP 服务端命令为空")
        self.command = command
        self.timeout = timeout
        self._env = env
        self.stderr_prefix = stderr_prefix
        self.proc: subprocess.Popen | None = None
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 1
        self._server_info: dict = {}
        self._tools: list[MCPTool] = []
        self.stderr_lines: list[str] = []

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> "MCPStdioClient":
        if self.proc is not None:
            return self
        args = shlex.split(self.command, posix=os.name != "nt")
        if os.name == "nt":
            # Windows 下 shlex 会吃掉反斜杠路径，用非 posix 模式重切
            args = shlex.split(self.command)
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except OSError as e:
            raise MCPError(f"无法启动 MCP 服务端 {args!r}：{e}") from e

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        return self

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 服务端混入的非协议输出，跳过而不是崩掉
                continue
            if isinstance(msg, dict):
                self._q.put(msg)
        self._q.put({"__eof__": True})

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            s = line.rstrip()
            if s:
                self.stderr_lines.append(s)

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def __enter__(self) -> "MCPStdioClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- JSON-RPC ----------------------------------------------------------
    def _send(self, obj: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("MCP 服务端未启动")
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"MCP 服务端连接已断开：{e}") from e

    def _request(self, method: str, params: dict | None = None) -> dict:
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            try:
                msg = self._q.get(timeout=self.timeout)
            except queue.Empty:
                raise MCPError(f"等待 {method} 响应超时（{self.timeout}s）") from None
            if msg.get("__eof__"):
                tail = " | ".join(self.stderr_lines[-3:])
                raise MCPError(f"MCP 服务端已退出（{method} 未响应）。stderr: {tail}")
            if msg.get("id") != rid:
                continue  # 不是我们要的响应（可能是别人的通知），继续等
            if "error" in msg:
                err = msg["error"] or {}
                raise MCPError(f"{method} 返回错误 {err.get('code')}：{err.get('message')}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- 高层接口 ----------------------------------------------------------
    def initialize(self) -> dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._server_info = result
        self._notify("notifications/initialized")
        return result

    def list_tools(self, refresh: bool = False) -> list[MCPTool]:
        if self._tools and not refresh:
            return self._tools
        result = self._request("tools/list")
        tools = result.get("tools") or []
        self._tools = [
            MCPTool(
                name=str(t.get("name") or ""),
                description=str(t.get("description") or ""),
                input_schema=t.get("inputSchema") or {},
            )
            for t in tools
            if isinstance(t, dict) and t.get("name")
        ]
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(f"工具 {name} 执行失败：{self._flatten(result)[:300]}")
        return self._flatten(result)

    @staticmethod
    def _flatten(result: dict) -> Any:
        """把 MCP 的 content 数组压成 Python 值；是 JSON 就解析成对象。"""
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(c.get("text", ""))
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
        if "structuredContent" in result:
            return result["structuredContent"]
        return result

    @property
    def server_info(self) -> dict:
        return self._server_info

    def tool_names(self) -> list[str]:
        return [t.name for t in self.list_tools()]


# ==========================================================================
# 把 MCP 工具适配成和 GitHubClient 一样的接口
# ==========================================================================
class MCPGitHubClient:
    """用 MCP 工具实现 PR 数据获取。

    工具名不做硬编码匹配 —— 不同 MCP Server 的命名差异很大。
    这里用「候选名优先级 + 子串模糊匹配」来发现工具，找不到就抛出
    带完整工具清单的错误，让人一眼知道该怎么配。
    """

    DIFF_TOOL_CANDIDATES = (
        "get_pull_request_diff",
        "pull_request_diff",
        "get_pr_diff",
        "get_pull_request_files",
        "list_pull_request_files",
    )
    PR_TOOL_CANDIDATES = ("get_pull_request", "pull_request_read", "get_pr")

    def __init__(self, client: MCPStdioClient, dry_run: bool = True) -> None:
        self.client = client
        self.dry_run = dry_run
        self.log: list[str] = []
        self._tool_map: dict[str, MCPTool] = {}

    # -- 工具发现 ----------------------------------------------------------
    def _discover(self) -> dict[str, MCPTool]:
        if self._tool_map:
            return self._tool_map
        self._tool_map = {t.name: t for t in self.client.list_tools()}
        return self._tool_map

    def _find(self, candidates: tuple[str, ...], purpose: str) -> MCPTool:
        tools = self._discover()
        for name in candidates:
            if name in tools:
                return tools[name]
        # 模糊匹配：候选名里的关键词出现在工具名中即算命中
        for name, tool in tools.items():
            low = name.lower()
            for cand in candidates:
                for kw in cand.split("_"):
                    if len(kw) > 3 and kw in low and "pull_request" in low:
                        return tool
        raise MCPError(
            f"未找到可用于「{purpose}」的 MCP 工具。"
            f"候选名：{list(candidates)}；服务端实际提供：{sorted(tools)}"
        )

    @staticmethod
    def _args(tool: MCPTool, ref) -> dict:
        """按工具声明的 inputSchema 组织参数，而不是猜。"""
        required = set(tool.required)
        args: dict[str, Any] = {}
        pool = {
            "owner": ref.owner,
            "repo": ref.repo,
            "pull_number": ref.number,
            "pullNumber": ref.number,
            "pr_number": ref.number,
            "number": ref.number,
            "prNumber": ref.number,
        }
        for k, v in pool.items():
            if k in (tool.input_schema.get("properties") or {}) or k in required:
                args[k] = v
        if not args:
            # schema 缺失时给一套最常见组合
            args = {"owner": ref.owner, "repo": ref.repo, "pull_number": ref.number}
        return args

    # -- 与 GitHubClient 对齐的接口 ----------------------------------------
    def get_pr(self, ref) -> dict:
        tool = self._find(self.PR_TOOL_CANDIDATES, "读取 PR 元信息")
        got = self.client.call_tool(tool.name, self._args(tool, ref))
        return got if isinstance(got, dict) else {"raw": got}

    def get_diff(self, ref) -> str:
        tool = self._find(self.DIFF_TOOL_CANDIDATES, "获取 PR diff")
        got = self.client.call_tool(tool.name, self._args(tool, ref))
        if isinstance(got, str):
            return got
        if isinstance(got, list):
            # files 列表形态：拼出近似 diff 的文本块
            parts: list[str] = []
            for item in got:
                if not isinstance(item, dict):
                    continue
                path = item.get("filename") or item.get("path") or "unknown"
                patch = item.get("patch") or item.get("diff") or ""
                parts.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{patch}")
            return "\n".join(parts)
        raise MCPError(f"工具 {tool.name} 返回的 diff 格式无法识别：{type(got).__name__}")

    def post_review(self, ref, body: str, comments: list[dict], event: str = "COMMENT", commit_id=None) -> dict:
        """MCP 侧只读不写：写操作一律走直连 REST。

        这是有意为之 —— 让一个自动工具通过 MCP 往 PR 上写内容，
        权限面太大且难以审计。MCP 用来读数据，写回走显式配置了 token 的 REST。
        """
        self.log.append("[mcp] post_review 被调用，但 MCP 通道只读，写操作请使用直连 REST 客户端")
        return {"skipped": True, "reason": "mcp-read-only"}


def build_diff_source(settings, ref=None):
    """按配置决定用 MCP 还是直连 REST。"""
    if getattr(settings, "mcp_server_cmd", ""):
        client = MCPStdioClient(settings.mcp_server_cmd).start()
        client.initialize()
        return MCPGitHubClient(client, dry_run=settings.dry_run)
    from .github import GitHubClient

    return GitHubClient(
        token=settings.github_token,
        api_base=settings.github_api_base,
        dry_run=settings.dry_run,
    )
