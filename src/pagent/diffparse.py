"""Unified diff 解析器。

只做一件事，但要做对：把 diff 文本还原成「文件 → hunk → 行」的结构，
并给每一行标好新文件行号。

行号是整个项目的地基 —— GitHub 的行内评论 API 要求提供新文件的绝对行号，
算错一位就会被服务端拒绝，或者更糟：评论挂到错误的代码行上，看起来像胡说八道。

这里有一个不明显但必须处理的歧义：在**裸 diff**（没有 `diff --git` 头，
例如 `difflib.unified_diff` 或 `git diff --no-index` 的输出）里，
「被删除的一行，其内容以 `-- ` 开头」和「下一个文件的 `--- ` 头」完全同形。

解决方案不是猜，而是**按 hunk 头声明的行数消费**：hunk 头里写明了
old/new 两侧各有多少行，消费完这个数字，hunk 就结束，后面的 `--- `
必然是文件头。这也是 git 官方 patch 解析器的做法。
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import DiffFile, Hunk, Line, LineKind

_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@(?P<hdr>.*)$"
)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")
_INDEX_RE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+")
_BINARY_RE = re.compile(r"^Binary files .* differ$|^GIT binary patch$")


def _strip_prefix(p: str) -> str:
    """去掉 git 的 a/ b/ 前缀，并处理带引号的路径。"""
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
        p = p.replace("\\t", "\t").replace("\\n", "\n").replace('\\"', '"')
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _parse_range(raw_start: str | None, raw_len: str | None) -> tuple[int, int]:
    """git 省略 ,len 时表示长度为 1。"""
    if raw_start is None:
        return 0, 0
    start = int(raw_start)
    length = 1 if raw_len is None else int(raw_len)
    return start, length


def parse_unified_diff(text: str) -> list[DiffFile]:
    """把一个完整的 unified diff 文本解析成 DiffFile 列表。

    容错原则：看不懂的行就地跳过，不抛异常。半份 diff 也比崩掉强。
    """
    if not text:
        return []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[DiffFile] = []

    cur_file: DiffFile | None = None
    cur_hunk: Hunk | None = None
    old_no = 0
    new_no = 0
    old_left = 0
    new_left = 0
    pending: tuple[str | None, str | None] = (None, None)

    def flush_hunk() -> None:
        nonlocal cur_hunk
        if cur_file is not None and cur_hunk is not None:
            cur_file.hunks.append(cur_hunk)
        cur_hunk = None

    def flush_file() -> None:
        nonlocal cur_file, pending
        flush_hunk()
        if cur_file is not None and (
            cur_file.hunks or cur_file.is_new or cur_file.is_deleted or cur_file.is_binary
        ):
            files.append(cur_file)
        cur_file = None
        pending = (None, None)

    def ensure_file() -> DiffFile:
        nonlocal cur_file
        if cur_file is None:
            path = pending[1] or pending[0] or "unknown"
            cur_file = DiffFile(path=_strip_prefix(path))
        return cur_file

    for raw in lines:
        # ---- 1. diff --git：开启新文件，先结算上一个 ----
        m = _DIFF_HEADER_RE.match(raw)
        if m:
            flush_file()
            pending = (_strip_prefix(m.group("a")), _strip_prefix(m.group("b")))
            continue

        # ---- 2. hunk 未开始时的头部行 ----
        if cur_hunk is None:
            if raw.startswith("index ") and _INDEX_RE.match(raw):
                continue

            if raw.startswith("--- "):
                # 到这里 hunk 一定已因计数用尽而关闭，所以这是文件头而非删除行
                flush_file()
                pending = (raw[4:].strip(), None)
                continue

            if raw.startswith("+++ "):
                newp = raw[4:].strip()
                oldp = pending[0]
                if newp and newp != "/dev/null":
                    path = _strip_prefix(newp)
                else:
                    path = _strip_prefix(oldp or pending[1] or "unknown")
                cur_file = DiffFile(path=path)
                if newp == "/dev/null":
                    cur_file.is_deleted = True
                if oldp is not None and oldp.strip() == "/dev/null":
                    cur_file.is_new = True
                if oldp and oldp.strip() != "/dev/null":
                    cur_file.old_path = _strip_prefix(oldp)
                continue

            if _BINARY_RE.match(raw):
                ensure_file().is_binary = True
                continue

        # ---- 3. hunk 头 ----
        hm = _HUNK_RE.match(raw)
        if hm and cur_hunk is None:
            f = ensure_file()
            old_start, old_len = _parse_range(hm.group("os"), hm.group("ol"))
            new_start, new_len = _parse_range(hm.group("ns"), hm.group("nl"))
            cur_hunk = Hunk(
                old_start=old_start,
                old_len=old_len,
                new_start=new_start,
                new_len=new_len,
                header=(hm.group("hdr") or "").strip(),
            )
            old_no, new_no = old_start, new_start
            old_left, new_left = old_len, new_len
            if old_left <= 0 and new_left <= 0:
                # 空 hunk（纯文件头变更），直接收掉
                cur_file.hunks.append(cur_hunk)
                cur_hunk = None
            continue

        # ---- 4. hunk 之外的行：忽略 ----
        if cur_hunk is None:
            continue

        # ---- 5. hunk 内容 ----
        if raw.startswith("\\"):
            cur_hunk.lines.append(Line(kind=LineKind.NO_NEWLINE, text=raw))
            continue  # 不消耗行数计数

        if raw.startswith("+"):
            cur_hunk.lines.append(Line(kind=LineKind.ADD, text=raw[1:], new_no=new_no))
            new_no += 1
            new_left -= 1
        elif raw.startswith("-"):
            cur_hunk.lines.append(Line(kind=LineKind.DEL, text=raw[1:], old_no=old_no))
            old_no += 1
            old_left -= 1
        else:
            body = raw[1:] if raw.startswith(" ") else raw
            cur_hunk.lines.append(
                Line(kind=LineKind.CONTEXT, text=body, old_no=old_no, new_no=new_no)
            )
            old_no += 1
            new_no += 1
            old_left -= 1
            new_left -= 1

        # 行数消费完毕 → hunk 结束。后续的 --- / +++ 必然是文件头。
        if old_left <= 0 and new_left <= 0:
            flush_hunk()

    flush_file()
    return files


def parse_diff_stats(text: str) -> dict:
    """快速统计，用于决定是否分片。"""
    files = parse_unified_diff(text)
    return {
        "files": len(files),
        "added": sum(f.added_line_count for f in files),
        "removed": sum(f.removed_line_count for f in files),
    }


def index_added_lines(files: Iterable[DiffFile]) -> set[tuple[str, int]]:
    """建立「(文件, 新行号)」集合。

    用途：校验 LLM 报出来的行号是否真实存在。
    这是压住幻觉的关键一步 —— 模型很擅长编一个看起来合理的行号。
    """
    out: set[tuple[str, int]] = set()
    for f in files:
        for ln in f.added_lines():
            if ln.new_no is not None:
                out.add((f.path, ln.new_no))
    return out
