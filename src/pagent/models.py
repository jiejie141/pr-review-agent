"""核心数据结构。

设计要点：所有审查意见最终都必须能落到「文件名 + 新文件行号」上，
这是把 LLM 的自由文本约束成可校验结构的第一道闸门。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Severity(str, Enum):
    """严重级别。用于排序与是否阻断 CI。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"high": 0, "medium": 1, "low": 2, "info": 3}[self.value]

    @property
    def blocks_ci(self) -> bool:
        return self is Severity.HIGH


class Category(str, Enum):
    """三个审查维度，与简历口径一致。"""

    SECURITY = "security"
    PERFORMANCE = "performance"
    CONVENTION = "convention"


class SourceKind(str, Enum):
    """意见来源。区分规则命中和模型判断，便于分别统计误报率。"""

    RULE = "rule"
    LLM = "llm"


class LineKind(str, Enum):
    CONTEXT = " "
    ADD = "+"
    DEL = "-"
    NO_NEWLINE = "\\"


@dataclass(frozen=True)
class Line:
    """diff 中的一行。new_no / old_no 为 None 表示该行在对应侧不存在。"""

    kind: LineKind
    text: str
    old_no: int | None = None
    new_no: int | None = None

    @property
    def is_added(self) -> bool:
        return self.kind is LineKind.ADD


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str = ""
    lines: list[Line] = field(default_factory=list)

    def added_lines(self) -> Iterator[Line]:
        for ln in self.lines:
            if ln.is_added:
                yield ln


@dataclass
class DiffFile:
    """一个被改动的文件。"""

    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_line_count(self) -> int:
        return sum(1 for h in self.hunks for _ in h.added_lines())

    @property
    def removed_line_count(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind is LineKind.DEL)

    @property
    def suffix(self) -> str:
        name = self.path.rsplit("/", 1)[-1]
        if "." not in name:
            return ""
        return "." + name.rsplit(".", 1)[-1].lower()

    def added_lines(self) -> Iterator[Line]:
        for h in self.hunks:
            yield from h.added_lines()

    def flat_lines(self) -> list[Line]:
        """把所有 hunk 的行压平成一个列表。上下文窗口的定位依赖这个顺序。"""
        return [ln for h in self.hunks for ln in h.lines]

    def context_slice(
        self, new_no: int, radius: int = 3, flat: list[Line] | None = None
    ) -> tuple[int, str]:
        """返回 (起始下标, 窗口文本)。

        需要返回起始下标的原因：跨行模式（如「循环内查库」）的匹配起点落在窗口内部的
        某一行上，必须能反查回它在扁平列表里的位置，才能把多个命中折叠成一条。
        否则循环体内每一行的窗口都会命中同一个 `for`，同一处缺陷被报 N 次。
        """
        lines = flat if flat is not None else self.flat_lines()
        idx = next(
            (i for i, ln in enumerate(lines) if ln.new_no == new_no and ln.is_added),
            None,
        )
        if idx is None:
            return 0, ""
        lo = max(0, idx - radius)
        hi = min(len(lines), idx + radius + 1)
        return lo, "\n".join(lines[i].text for i in range(lo, hi))

    def context_window(self, new_no: int, radius: int = 3) -> str:
        """取某一行前后的上下文，供规则做跨行判断或给 LLM 看。"""
        return self.context_slice(new_no, radius)[1]

    def render(self, max_lines: int | None = None) -> str:
        """还原成可读的 diff 片段，用于喂给 LLM。"""
        out: list[str] = [f"### 文件: {self.path}"]
        if self.is_new:
            out.append("(新增文件)")
        if self.is_deleted:
            out.append("(删除文件)")
        n = 0
        for h in self.hunks:
            out.append(
                f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@ {h.header}".rstrip()
            )
            for ln in h.lines:
                if ln.kind is LineKind.NO_NEWLINE:
                    continue
                prefix = " " if ln.new_no is None and ln.old_no is not None and ln.kind is LineKind.DEL else ln.kind.value
                mark = f"{ln.new_no}:" if ln.new_no is not None else "  -"
                out.append(f"{mark} {prefix}{ln.text}")
                n += 1
                if max_lines is not None and n >= max_lines:
                    out.append("... (已截断)")
                    return "\n".join(out)
        return "\n".join(out)


@dataclass
class Finding:
    """一条审查意见。line 一定是新文件行号，否则不能作为行内评论发出。"""

    rule_id: str
    title: str
    category: Category
    severity: Severity
    file: str
    line: int | None
    evidence: str = ""
    suggestion: str = ""
    confidence: float = 1.0
    source: SourceKind = SourceKind.RULE
    anchors_ok: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """去重指纹：同一行同一维度只保留一条。"""
        raw = f"{self.file}|{self.line}|{self.category.value}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def is_actionable(self) -> bool:
        """只有锚定到具体行、且判据明确的意见才允许作为行内评论发出。"""
        return self.line is not None and self.anchors_ok

    def render(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else f"{self.file} (无行号)"
        icon = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "⚪"}[self.severity.value]
        lines = [
            f"{icon} **[{self.severity.value.upper()}] {self.title}**  `{self.rule_id}`",
            f"- 位置：`{loc}`",
            f"- 维度：{self.category.value} · 来源：{self.source.value} · 置信度：{self.confidence:.2f}",
        ]
        if self.evidence:
            lines.append(f"- 证据：`{self.evidence.strip()[:200]}`")
        if self.suggestion:
            lines.append(f"- 建议：{self.suggestion.strip()}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "category": self.category.value,
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "confidence": round(self.confidence, 3),
            "source": self.source.value,
            "actionable": self.is_actionable,
        }


@dataclass
class ReviewStats:
    files: int = 0
    added_lines: int = 0
    shards: int = 0
    rule_findings: int = 0
    llm_findings: int = 0
    dropped_unanchored: int = 0
    merged_duplicates: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        d = {
            "files": self.files,
            "added_lines": self.added_lines,
            "shards": self.shards,
            "rule_findings": self.rule_findings,
            "llm_findings": self.llm_findings,
            "dropped_unanchored": self.dropped_unanchored,
            "merged_duplicates": self.merged_duplicates,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        return d


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    stats: ReviewStats = field(default_factory=ReviewStats)
    pr_title: str = ""
    pr_url: str = ""
    errors: list[str] = field(default_factory=list)

    def sort(self) -> "ReviewResult":
        self.findings.sort(
            key=lambda f: (f.severity.rank, f.file, f.line if f.line is not None else 10**9)
        )
        return self

    def actionable(self) -> list[Finding]:
        return [f for f in self.findings if f.is_actionable]

    def by_category(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.category.value, []).append(f)
        return out

    def count_by_severity(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    def should_block(self) -> bool:
        return any(f.severity.blocks_ci for f in self.actionable())


def iter_added(files: Iterable[DiffFile]) -> Iterator[tuple[DiffFile, Line]]:
    for f in files:
        for ln in f.added_lines():
            yield f, ln
