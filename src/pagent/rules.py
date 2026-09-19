"""规则引擎：在 diff 的新增行上跑缺陷模式。

一个刻意的设计约束：**只锚定新增行**。

原因在 GitHub 侧 —— 行内评论要求提供 `(path, line, side=RIGHT)`，且该行必须
出现在 diff 里。历史行、被删除行都无法作为评论锚点。所以规则命中一律上报
「触发的那个新增行」，宁可锚点粗糙一点，也不产生发不出去的评论。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Category, DiffFile, Finding, Line, Severity, SourceKind
from .patterns import PATTERNS, Pattern, get_pattern


@dataclass
class RuleHit:
    pattern_id: str
    file: str
    line: int
    evidence: str


@dataclass
class RuleReport:
    findings: list[Finding] = field(default_factory=list)
    skipped_binary: int = 0
    skipped_deleted: int = 0
    scanned_lines: int = 0
    hits_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.findings)


MAX_EVIDENCE = 240

# 各语言的单行注释起始标记
COMMENT_MARKERS: dict[str, str] = {
    ".py": "#",
    ".rb": "#",
    ".js": "//",
    ".jsx": "//",
    ".ts": "//",
    ".tsx": "//",
    ".java": "//",
    ".kt": "//",
    ".go": "//",
    ".php": "//",
    ".sql": "--",
}


def comment_start(line: str, suffix: str) -> int | None:
    """返回该行注释起始的下标；没有注释返回 None。

    做一个最小但正确的处理：跳过字符串字面量内部的 `#` / `//`，
    否则 `print("a # b")` 会被误判成后半行是注释。
    不追求完整的词法分析，够用即可。
    """
    marker = COMMENT_MARKERS.get(suffix)
    if not marker:
        return None
    in_single = in_double = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and line.startswith(marker, i):
            return i
        i += 1
    return None


def _clean(text: str) -> str:
    return text.strip().replace("\n", " ⏎ ")[:MAX_EVIDENCE]


class RuleEngine:
    """确定性检查层。零 token 成本、结果可复现，是评测的基线。"""

    def __init__(
        self,
        patterns: tuple[Pattern, ...] | None = None,
        disabled: set[str] | None = None,
        min_confidence: float = 0.0,
    ) -> None:
        pats = patterns if patterns is not None else PATTERNS
        off = disabled or set()
        self.patterns = tuple(p for p in pats if p.id not in off and p.confidence >= min_confidence)
        self.context_radius = 4

    # -- 单文件 ------------------------------------------------------------
    def analyze_file(self, f: DiffFile) -> list[Finding]:
        findings: list[Finding] = []
        if f.is_binary or f.is_deleted:
            return findings
        candidates = [p for p in self.patterns if p.applies_to(f.path)]
        if not candidates:
            return findings

        seen: set[tuple[str, int]] = set()
        suffix = f.suffix
        flat = f.flat_lines()
        for ln in f.added_lines():
            if ln.new_no is None:
                continue
            # 注释起始位置。命中落在注释里的规则会被跳过（CONV304 等显式关闭）——
            # 典型场景：注释写着「禁止使用 os.system(cmd)」是反例说明，不是缺陷。
            cstart = comment_start(ln.text, suffix)
            window: str | None = None  # 懒加载：只有真需要上下文的模式才算
            base = 0
            for p in candidates:
                anchor_line = ln.new_no
                if p.needs_context:
                    if window is None:
                        base, window = f.context_slice(
                            ln.new_no, radius=self.context_radius, flat=flat
                        )
                        if not window:
                            window = ln.text
                    target = window
                else:
                    # 单行模式必须只看当行 —— 一旦拿多行窗口去匹配，
                    # `^\s*except\s*:\s*$` 这类带锚点的规则会全部失效
                    target = ln.text
                m = p.match(target)
                if not m:
                    continue
                if p.needs_context:
                    # 把匹配起点映射回具体的行，并以该行作为锚点。
                    # 循环体内每一行的窗口都会命中同一个 `for`，锚到匹配起点后
                    # 这些重复会收敛成同一个 (规则, 行号)，被下面的 seen 去掉。
                    offset = target[: m.start()].count("\n")
                    idx = base + offset
                    if 0 <= idx < len(flat):
                        cand = flat[idx]
                        if cand.is_added and cand.new_no is not None:
                            anchor_line = cand.new_no
                else:
                    # 窗口模式下无法把匹配位置映射回行内注释位置，跳过该判断
                    if cstart is not None and m.start() >= cstart and p.ignore_comments:
                        continue
                key = (p.id, anchor_line)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(self._to_finding(p, f, ln, m, anchor_line))
        return findings

    def _to_finding(self, p: Pattern, f: DiffFile, ln: Line, m, anchor_line: int | None = None) -> Finding:
        evidence = _clean(m.group(0) if m.group(0) else ln.text)
        return Finding(
            rule_id=p.id,
            title=p.title,
            category=p.category,
            severity=p.severity,
            file=f.path,
            line=anchor_line if anchor_line is not None else ln.new_no,
            evidence=evidence,
            suggestion=p.suggestion,
            confidence=p.confidence,
            source=SourceKind.RULE,
            anchors_ok=True,
            extra={"pattern_description": p.description},
        )

    # -- 全量 --------------------------------------------------------------
    def analyze(self, files: list[DiffFile]) -> RuleReport:
        report = RuleReport()
        for f in files:
            if f.is_binary:
                report.skipped_binary += 1
                continue
            if f.is_deleted:
                report.skipped_deleted += 1
                continue
            report.scanned_lines += f.added_line_count
            for fi in self.analyze_file(f):
                report.findings.append(fi)
                report.hits_by_rule[fi.rule_id] = report.hits_by_rule.get(fi.rule_id, 0) + 1
        return report


def annotate_source_lines(files: list[DiffFile]) -> dict[tuple[str, int], str]:
    """(文件, 新行号) → 原始行文本。供报告层回显上下文。"""
    out: dict[tuple[str, int], str] = {}
    for f in files:
        for ln in f.added_lines():
            if ln.new_no is not None:
                out[(f.path, ln.new_no)] = ln.text
    return out


def summarize_by_rule(findings: list[Finding]) -> list[tuple[str, str, Category, Severity, int]]:
    """按规则聚合，输出「命中次数」排行，方便看哪类问题最集中。"""
    agg: dict[str, tuple[str, Category, Severity, int]] = {}
    for f in findings:
        title, cat, sev, n = agg.get(f.rule_id, (f.title, f.category, f.severity, 0))
        agg[f.rule_id] = (title, cat, sev, n + 1)
    rows = [(rid, t, c, s, n) for rid, (t, c, s, n) in agg.items()]
    rows.sort(key=lambda r: (r[3].rank, -r[4], r[0]))
    return rows


def rule_coverage() -> dict[str, int]:
    """规则库的覆盖面统计，写进报告首页。"""
    out: dict[str, int] = {}
    for p in PATTERNS:
        out[p.id] = out.get(p.id, 0) + 1
    return out


__all__ = [
    "RuleEngine",
    "RuleReport",
    "RuleHit",
    "annotate_source_lines",
    "summarize_by_rule",
    "rule_coverage",
    "get_pattern",
]
