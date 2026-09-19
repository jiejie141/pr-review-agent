"""GitHub 客户端测试。

不发真实网络请求：把 `_request` 换成桩函数来验证参数组装、dry-run 与降级逻辑。
"""

import json

import pytest

from pagent.github import GitHubClient, GitHubError, PRRef, parse_pr_ref


# ---------------------------------------------------------------- PR 引用解析
def test_parse_owner_repo_hash_number():
    ref = parse_pr_ref("octocat/hello-world#42")
    assert ref.owner == "octocat" and ref.repo == "hello-world" and ref.number == 42
    assert str(ref) == "octocat/hello-world#42"
    assert ref.slug == "octocat/hello-world"


def test_parse_slash_form():
    ref = parse_pr_ref("octocat/hello-world/7")
    assert ref.number == 7


def test_parse_url_form():
    ref = parse_pr_ref("https://github.com/octocat/hello-world/pull/123")
    assert (ref.owner, ref.repo, ref.number) == ("octocat", "hello-world", 123)


def test_parse_url_with_query_and_fragment():
    ref = parse_pr_ref("https://github.com/o/r/pull/9/files#diff-abc")
    assert ref.number == 9


def test_parse_invalid_raises():
    for bad in ("", "just-a-name", "owner/repo", "http://example.com/x"):
        with pytest.raises(GitHubError):
            parse_pr_ref(bad)


# ---------------------------------------------------------------- 请求桩
class StubClient(GitHubClient):
    def __init__(self, responses=None, **kw):
        super().__init__(token="t" * 40, **kw)
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None, str]] = []

    def _request(self, method, path, *, body=None, accept="application/vnd.github+json", raw=False):
        self.calls.append((method, path, body, accept))
        key = (method, path.split("?")[0])
        val = self.responses.get(key, self.responses.get(method, None))
        if isinstance(val, Exception):
            raise val
        return val


def test_get_diff_uses_diff_media_type():
    c = StubClient({("GET", "/repos/o/r/pulls/1"): "--- a/x\n+++ b/x\n"})
    got = c.get_diff(PRRef("o", "r", 1))
    assert got.startswith("--- a/x")
    assert c.calls[0][3] == "application/vnd.github.v3.diff"


def test_get_pr_returns_dict():
    c = StubClient({("GET", "/repos/o/r/pulls/1"): {"number": 1, "title": "T"}})
    assert c.get_pr(PRRef("o", "r", 1))["title"] == "T"


def test_list_files_paginates():
    """注意：不能用 `"page=1" in path` 判页码 —— per_page=100 里就含 "page=1" 子串，
    会让桩函数永远返回同一页，测试直接死循环。用显式页码计数。"""
    page1 = [{"filename": f"f{i}.py"} for i in range(100)]
    seen_pages: list[str] = []

    class Paged(StubClient):
        def _request(self, method, path, *, body=None, accept="application/vnd.github+json", raw=False):
            self.calls.append((method, path, body, accept))
            qs = path.split("?", 1)[-1]
            page_no = dict(kv.split("=") for kv in qs.split("&") if "=" in kv).get("page", "1")
            seen_pages.append(page_no)
            return page1 if page_no == "1" else []

    c = Paged()
    got = c.list_files(PRRef("o", "r", 1))
    assert len(got) == 100
    assert seen_pages == ["1", "2"]
    assert len(c.calls) == 2


def test_head_sha_extracted():
    c = StubClient({("GET", "/repos/o/r/pulls/1"): {"head": {"sha": "abc123"}}})
    assert c.head_sha(PRRef("o", "r", 1)) == "abc123"


# ---------------------------------------------------------------- dry-run
def test_dry_run_logs_without_calling_api():
    c = StubClient(dry_run=True)
    resp = c.post_review(PRRef("o", "r", 1), "body", [{"path": "a.py", "line": 1, "body": "x"}])
    assert resp["dry_run"] is True
    assert c.calls == []
    assert any("dry-run" in line for line in c.log)


def test_dry_run_reports_comment_count():
    c = StubClient(dry_run=True)
    resp = c.post_review(PRRef("o", "r", 1), "b", [{"path": "a.py", "line": 1, "body": "x"}] * 3)
    assert resp["comments"] == 3


# ---------------------------------------------------------------- 写回与降级
def test_post_review_success_path():
    class Ok(StubClient):
        def _request(self, method, path, *, body=None, accept="application/vnd.github+json", raw=False):
            self.calls.append((method, path, body, accept))
            return {"id": 99}

    c = Ok(dry_run=False)
    resp = c.post_review(PRRef("o", "r", 3), "报告", [{"path": "a.py", "line": 2, "body": "c"}])
    assert resp["id"] == 99
    assert c.calls[0][0] == "POST"
    assert c.calls[0][1] == "/repos/o/r/pulls/3/reviews"
    assert c.calls[0][2]["comments"][0]["path"] == "a.py"


def test_post_review_falls_back_to_per_comment():
    """整批被拒（常见于某条评论行号不在 diff 内）时应降级为逐条提交。"""

    class Fallback(StubClient):
        def _request(self, method, path, *, body=None, accept="application/vnd.github+json", raw=False):
            self.calls.append((method, path, body, accept))
            if path.endswith("/reviews") and body and body.get("comments"):
                raise GitHubError("请求被拒绝（422）：line must be part of the diff")
            return {"ok": True}

    c = Fallback(dry_run=False)
    resp = c.post_review(
        PRRef("o", "r", 3),
        "报告",
        [{"path": "a.py", "line": 2, "body": "c1"}, {"path": "b.py", "line": 5, "body": "c2"}],
    )
    assert resp["posted"] == 2
    assert "/comments" in c.calls[-1][1]
    assert any("降级" in line for line in c.log)


def test_post_review_reports_per_comment_failures():
    class Partial(StubClient):
        def _request(self, method, path, *, body=None, accept="application/vnd.github+json", raw=False):
            self.calls.append((method, path, body, accept))
            if path.endswith("/reviews"):
                raise GitHubError("422 nope")
            if (body or {}).get("path") == "bad.py":
                raise GitHubError("422 line not in diff")
            return {"ok": True}

    c = Partial(dry_run=False)
    resp = c.post_review(
        PRRef("o", "r", 3),
        "报告",
        [{"path": "ok.py", "line": 1, "body": "a"}, {"path": "bad.py", "line": 9, "body": "b"}],
    )
    assert resp["posted"] == 1
    assert len(resp["failed"]) == 1
    assert "bad.py" in resp["failed"][0]


# ---------------------------------------------------------------- 错误映射
@pytest.mark.parametrize(
    "code,keyword",
    [(401, "认证失败"), (403, "拒绝访问"), (404, "不存在"), (422, "被拒绝")],
)
def test_http_errors_map_to_readable_messages(code, keyword):
    import urllib.error

    c = GitHubClient(token="t" * 40)

    def boom(*a, **kw):
        raise urllib.error.HTTPError("u", code, "msg", {}, None)

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        with pytest.raises(GitHubError) as e:
            c._request("GET", "/x")
        assert keyword in str(e.value)
        assert str(code) in str(e.value)
    finally:
        urllib.request.urlopen = orig


def test_network_error_maps_to_github_error():
    import urllib.error
    import urllib.request

    c = GitHubClient(token="t" * 40)

    def boom(*a, **kw):
        raise urllib.error.URLError("dns fail")

    orig = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        with pytest.raises(GitHubError) as e:
            c._request("GET", "/x")
        assert "网络异常" in str(e.value)
    finally:
        urllib.request.urlopen = orig


def test_headers_include_auth_and_version():
    c = GitHubClient(token="secret-token-value")
    h = c._headers()
    assert h["Authorization"] == "Bearer secret-token-value"
    assert h["X-GitHub-Api-Version"] == "2022-11-28"


def test_headers_omit_auth_without_token():
    assert "Authorization" not in GitHubClient(token="")._headers()


def test_client_defaults_to_dry_run():
    assert GitHubClient().dry_run is True
