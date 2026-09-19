"""MCP stdio 客户端测试。

用一个真实的子进程（tests/fake_mcp_server.py）验证协议交互，
而不是把传输层打桩 —— 否则测不到「非 JSON 噪声行」「通知无 id」这些真实坑。
"""

import sys
from pathlib import Path

import pytest

from pagent.github import PRRef
from pagent.mcp import (
    MCPError,
    MCPGitHubClient,
    MCPStdioClient,
    MCPTool,
    PROTOCOL_VERSION,
)

SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"


def cmd() -> str:
    return f'"{sys.executable}" "{SERVER}"'


@pytest.fixture
def client():
    c = MCPStdioClient(cmd(), timeout=20)
    c.start().initialize()
    yield c
    c.close()


# ---------------------------------------------------------------- 传输层
def test_initialize_returns_server_info(client):
    assert client.server_info["serverInfo"]["name"] == "fake-github-mcp"
    assert client.server_info["protocolVersion"] == PROTOCOL_VERSION


def test_non_json_noise_line_is_ignored(client):
    """假服务端第一行就输出了垃圾，客户端仍应正常握手。"""
    assert client.list_tools()


def test_list_tools_returns_typed_objects(client):
    tools = client.list_tools()
    assert [t.name for t in tools] == ["get_pull_request", "get_pull_request_files"]
    assert all(isinstance(t, MCPTool) for t in tools)


def test_list_tools_is_cached(client):
    first = client.list_tools()
    second = client.list_tools()
    assert first is second


def test_tool_input_schema_and_required_parsed(client):
    tool = {t.name: t for t in client.list_tools()}["get_pull_request"]
    assert tool.required == ["owner", "repo", "pull_number"]
    assert "pull_number" in tool.input_schema["properties"]


def test_call_tool_parses_json_content(client):
    got = client.call_tool("get_pull_request", {"owner": "octocat", "repo": "hello-world", "pull_number": 7})
    assert isinstance(got, dict)
    assert got["title"] == "Fix the thing"


def test_call_tool_returns_list_payload(client):
    got = client.call_tool("get_pull_request_files", {"owner": "octocat", "repo": "hello-world", "pull_number": 7})
    assert isinstance(got, list)
    assert got[0]["filename"] == "app/db.py"


def test_call_unknown_tool_raises(client):
    with pytest.raises(MCPError) as e:
        client.call_tool("nope", {})
    assert "nope" in str(e.value)


def test_tool_names_helper(client):
    assert set(client.tool_names()) == {"get_pull_request", "get_pull_request_files"}


def test_context_manager_closes_process():
    with MCPStdioClient(cmd(), timeout=20) as c:
        c.initialize()
        assert c.list_tools()
    assert c.proc is None


def test_start_empty_command_raises():
    with pytest.raises(MCPError):
        MCPStdioClient("")


def test_start_bad_command_raises():
    with pytest.raises(MCPError):
        MCPStdioClient("definitely-not-a-real-binary-xyz").start()


def test_request_after_close_raises():
    c = MCPStdioClient(cmd(), timeout=20)
    c.start().initialize()
    c.close()
    with pytest.raises(MCPError):
        c.list_tools()


# ---------------------------------------------------------------- 适配层
def test_find_tool_prefers_exact_candidate(client):
    g = MCPGitHubClient(client)
    tool = g._find(("get_pull_request",), "读取 PR")
    assert tool.name == "get_pull_request"


def test_find_tool_falls_back_to_fuzzy_match(client):
    g = MCPGitHubClient(client)
    tool = g._find(("get_pr_diff_files_here",), "获取文件")
    assert "pull_request" in tool.name


def test_find_tool_error_lists_available_tools(client):
    g = MCPGitHubClient(client)
    g._discover()
    # 把所有已发现工具清掉，模拟服务端没提供可用工具
    g._tool_map = {"unrelated_tool": MCPTool(name="unrelated_tool")}
    with pytest.raises(MCPError) as e:
        g._find(("get_pull_request",), "读取 PR")
    assert "unrelated_tool" in str(e.value)


def test_args_built_from_schema(client):
    g = MCPGitHubClient(client)
    tool = {t.name: t for t in client.list_tools()}["get_pull_request"]
    args = g._args(tool, PRRef("octocat", "hello-world", 7))
    assert args == {"owner": "octocat", "repo": "hello-world", "pull_number": 7}


def test_get_pr_via_mcp(client):
    g = MCPGitHubClient(client)
    pr = g.get_pr(PRRef("octocat", "hello-world", 7))
    assert pr["number"] == 7


def test_get_diff_synthesizes_from_files(client):
    g = MCPGitHubClient(client)
    diff = g.get_diff(PRRef("octocat", "hello-world", 7))
    assert "diff --git a/app/db.py b/app/db.py" in diff
    assert "DB_PASSWORD" in diff


def test_get_diff_is_parseable_by_diff_parser(client):
    from pagent.diffparse import parse_unified_diff

    g = MCPGitHubClient(client)
    files = parse_unified_diff(g.get_diff(PRRef("octocat", "hello-world", 7)))
    assert files and files[0].path == "app/db.py"
    assert files[0].added_line_count == 1


def test_mcp_client_is_read_only_for_writes(client):
    g = MCPGitHubClient(client)
    resp = g.post_review(PRRef("o", "r", 1), "body", [{"path": "a.py", "line": 1, "body": "x"}])
    assert resp["skipped"] is True
    assert resp["reason"] == "mcp-read-only"
    assert any("只读" in line for line in g.log)


def test_diff_payload_recognizes_patch_key():
    tool_payload = [{"path": "x.py", "diff": "@@ -1 +1,2 @@\n a\n+b\n"}]

    class FakeClient:
        def list_tools(self):
            return [MCPTool(name="get_pull_request_files", input_schema={})]

        def call_tool(self, name, args):
            return tool_payload

    g = MCPGitHubClient(FakeClient())
    diff = g.get_diff(PRRef("o", "r", 1))
    assert "b/x.py" in diff
