"""GitHub 客户端（直连 REST）。

标准库实现，不依赖 PyGithub / requests。理由和配置层一样：运行时零依赖
意味着 CI 里不需要 pip install 就能跑，评审者也不会被依赖版本问题挡住。

默认 dry_run=True —— 写操作必须显式开启。自动审查工具最忌讳的事就是
在别人没准备好时往 PR 上刷评论。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_PR_REF_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:#|/)(?P<num>\d+)$")
_PR_URL_RE = re.compile(
    r"https?://[\w.-]+/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<num>\d+)"
)


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_ref(text: str) -> PRRef:
    """支持三种写法：owner/repo#123、owner/repo/123、PR 页面 URL。"""
    s = (text or "").strip()
    m = _PR_URL_RE.search(s)
    if m:
        return PRRef(m.group("owner"), m.group("repo"), int(m.group("num")))
    m = _PR_REF_RE.match(s)
    if m:
        return PRRef(m.group("owner"), m.group("repo"), int(m.group("num")))
    raise GitHubError(
        f"无法识别 PR 标识：{text!r}（支持 owner/repo#123 或 PR 页面链接）"
    )


@dataclass
class GitHubClient:
    token: str = ""
    api_base: str = "https://api.github.com"
    dry_run: bool = True
    timeout: int = 30
    user_agent: str = "pr-review-agent/1.0"
    log: list[str] = field(default_factory=list)

    # -- 底层请求 ----------------------------------------------------------
    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(accept))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                if raw:
                    return payload
                return json.loads(payload) if payload.strip() else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if e.code == 401:
                raise GitHubError(f"GitHub 认证失败（401）：token 无效或已过期。{detail}") from e
            if e.code == 403:
                raise GitHubError(
                    f"GitHub 拒绝访问（403）：权限不足或触发限流。{detail}"
                ) from e
            if e.code == 404:
                raise GitHubError(f"资源不存在（404）：{path} —— 检查仓库名、PR 号与 token 权限。{detail}") from e
            if e.code == 422:
                raise GitHubError(f"请求被拒绝（422）：{detail}") from e
            raise GitHubError(f"HTTP {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise GitHubError(f"网络异常：{e}") from e

    # -- 读操作 ------------------------------------------------------------
    def get_pr(self, ref: PRRef) -> dict:
        return self._request("GET", f"/repos/{ref.slug}/pulls/{ref.number}")

    def get_diff(self, ref: PRRef) -> str:
        """拿 PR 的 unified diff。注意这是 v3 的媒体类型，不走 JSON。"""
        return self._request(
            "GET",
            f"/repos/{ref.slug}/pulls/{ref.number}",
            accept="application/vnd.github.v3.diff",
            raw=True,
        )

    def list_files(self, ref: PRRef, per_page: int = 100) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            got = self._request(
                "GET",
                f"/repos/{ref.slug}/pulls/{ref.number}/files?per_page={per_page}&page={page}",
            )
            if not isinstance(got, list) or not got:
                break
            out.extend(got)
            if len(got) < per_page:
                break
            page += 1
        return out

    def head_sha(self, ref: PRRef) -> str:
        pr = self.get_pr(ref)
        head = pr.get("head") or {}
        return str(head.get("sha") or "")

    # -- 写操作 ------------------------------------------------------------
    def post_review(
        self,
        ref: PRRef,
        body: str,
        comments: list[dict],
        event: str = "COMMENT",
        commit_id: str | None = None,
    ) -> dict:
        """提交一次 review。

        comments 每项形如 {"path": "a.py", "line": 12, "side": "RIGHT", "body": "..."}

        整批提交失败（通常是某条评论的行号不在 diff 内触发 422）时，
        降级为逐条提交 —— 一条坏评论不该让整份审查结果白跑。
        """
        if self.dry_run:
            self.log.append(
                f"[dry-run] 将提交 review 到 {ref}：正文 {len(body)} 字，行内评论 {len(comments)} 条，event={event}"
            )
            for c in comments[:50]:
                self.log.append(f"[dry-run]   comment {c.get('path')}:{c.get('line')} ({len(c.get('body',''))} 字)")
            return {"dry_run": True, "comments": len(comments)}

        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        if commit_id:
            payload["commit_id"] = commit_id

        try:
            resp = self._request("POST", f"/repos/{ref.slug}/pulls/{ref.number}/reviews", body=payload)
            self.log.append(f"已提交 review（含 {len(comments)} 条行内评论）")
            return resp if isinstance(resp, dict) else {"ok": True}
        except GitHubError as e:
            if not comments:
                raise
            self.log.append(f"整批提交失败，降级为逐条提交：{e}")

        # 降级：先发正文，再逐条试行内评论
        ok, failed = 0, []
        try:
            self._request(
                "POST",
                f"/repos/{ref.slug}/pulls/{ref.number}/reviews",
                body={"body": body, "event": event},
            )
        except GitHubError as e:
            self.log.append(f"正文提交也失败：{e}")

        for c in comments:
            try:
                self._request(
                    "POST",
                    f"/repos/{ref.slug}/pulls/{ref.number}/comments",
                    body={
                        "body": c.get("body", ""),
                        "path": c.get("path", ""),
                        "line": c.get("line"),
                        "side": "RIGHT",
                        **({"commit_id": commit_id} if commit_id else {}),
                    },
                )
                ok += 1
            except GitHubError as e:
                failed.append(f"{c.get('path')}:{c.get('line')} → {e}")
        self.log.append(f"降级提交完成：成功 {ok} 条，失败 {len(failed)} 条")
        return {"ok": True, "posted": ok, "failed": failed}

    # -- 便捷 --------------------------------------------------------------
    def repo_tree_has(self, ref: PRRef, path: str) -> bool:
        try:
            self._request("GET", f"/repos/{ref.slug}/contents/{urllib.parse.quote(path)}")
            return True
        except GitHubError:
            return False


def git_local_diff(base: str = "HEAD~1", head: str = "HEAD") -> str:
    """本地对比，用于在没连 GitHub 的情况下试跑。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "diff", "--no-color", "--unified=3", base, head],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitHubError(f"执行 git diff 失败：{e}") from e
    if out.returncode != 0:
        raise GitHubError(f"git diff 退出码 {out.returncode}：{(out.stderr or '')[:300]}")
    return out.stdout
