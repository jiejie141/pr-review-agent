"""数据结构测试。"""

from pagent.models import (
    Category,
    DiffFile,
    Finding,
    Hunk,
    Line,
    LineKind,
    ReviewResult,
    ReviewStats,
    Severity,
    SourceKind,
    iter_added,
)


# ---------------------------------------------------------------- 枚举
def test_severity_rank_orders_high_first():
    assert Severity.HIGH.rank < Severity.MEDIUM.rank < Severity.LOW.rank < Severity.INFO.rank


def test_only_high_severity_blocks_ci():
    assert Severity.HIGH.blocks_ci is True
    for s in (Severity.MEDIUM, Severity.LOW, Severity.INFO):
        assert s.blocks_ci is False


def test_category_values_match_resume_wording():
    assert {c.value for c in Category} == {"security", "performance", "convention"}


# ---------------------------------------------------------------- Line / Hunk
def test_line_is_added_flag():
    assert Line(kind=LineKind.ADD, text="x", new_no=1).is_added is True
    assert Line(kind=LineKind.DEL, text="x", old_no=1).is_added is False
    assert Line(kind=LineKind.CONTEXT, text="x", old_no=1, new_no=1).is_added is False


def test_hunk_added_lines_filters():
    h = Hunk(old_start=1, old_len=2, new_start=1, new_len=3)
    h.lines = [
        Line(kind=LineKind.CONTEXT, text="a", old_no=1, new_no=1),
        Line(kind=LineKind.ADD, text="b", new_no=2),
        Line(kind=LineKind.ADD, text="c", new_no=3),
        Line(kind=LineKind.DEL, text="d", old_no=2),
    ]
    assert [ln.text for ln in h.added_lines()] == ["b", "c"]


# ---------------------------------------------------------------- DiffFile
def make_file() -> DiffFile:
    f = DiffFile(path="app/x.py")
    h = Hunk(old_start=1, old_len=2, new_start=1, new_len=4)
    h.lines = [
        Line(kind=LineKind.CONTEXT, text="import os", old_no=1, new_no=1),
        Line(kind=LineKind.ADD, text="", new_no=2),
        Line(kind=LineKind.ADD, text="def f():", new_no=3),
        Line(kind=LineKind.ADD, text="    return 1", new_no=4),
        Line(kind=LineKind.DEL, text="pass", old_no=2),
    ]
    f.hunks.append(h)
    return f


def test_diff_file_counts():
    f = make_file()
    assert f.added_line_count == 3
    assert f.removed_line_count == 1


def test_diff_file_suffix():
    assert DiffFile(path="a/b/Handler.java").suffix == ".java"
    assert DiffFile(path="Makefile").suffix == ""
    assert DiffFile(path="x.tar.gz").suffix == ".gz"


def test_context_window_around_added_line():
    f = make_file()
    win = f.context_window(3, radius=2)
    assert "def f():" in win
    assert "import os" in win


def test_context_window_returns_empty_when_line_not_added():
    f = make_file()
    assert f.context_window(1) == ""


def test_render_includes_path_and_numbers():
    text = make_file().render()
    assert "### 文件: app/x.py" in text
    assert "3: +def f():" in text


def test_iter_added_yields_pairs():
    pairs = list(iter_added([make_file()]))
    assert len(pairs) == 3
    assert all(isinstance(p[1], Line) for p in pairs)


# ---------------------------------------------------------------- Finding
def test_finding_fingerprint_groups_same_line_and_category():
    a = Finding(rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH, file="f.py", line=3)
    b = Finding(rule_id="B", title="u", category=Category.SECURITY, severity=Severity.LOW, file="f.py", line=3)
    c = Finding(rule_id="C", title="v", category=Category.CONVENTION, severity=Severity.LOW, file="f.py", line=3)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_finding_fingerprint_differs_by_line():
    a = Finding(rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH, file="f.py", line=3)
    b = Finding(rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH, file="f.py", line=4)
    assert a.fingerprint != b.fingerprint


def test_finding_is_actionable_requires_line_and_anchor():
    ok = Finding(rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH, file="f.py", line=1)
    no_line = Finding(rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH, file="f.py", line=None)
    bad_anchor = Finding(
        rule_id="A", title="t", category=Category.SECURITY, severity=Severity.HIGH,
        file="f.py", line=1, anchors_ok=False,
    )
    assert ok.is_actionable and not no_line.is_actionable and not bad_anchor.is_actionable


def test_finding_render_contains_everything_needed_for_review():
    f = Finding(
        rule_id="SEC101", title="SQL 注入", category=Category.SECURITY, severity=Severity.HIGH,
        file="a.py", line=9, evidence="execute(x)", suggestion="参数化", confidence=0.85,
    )
    text = f.render()
    assert "SEC101" in text
    assert "a.py:9" in text
    assert "参数化" in text
    assert "HIGH" in text


def test_finding_render_without_line_marks_it():
    f = Finding(rule_id="X", title="t", category=Category.SECURITY, severity=Severity.LOW, file="a.py", line=None)
    assert "无行号" in f.render()


def test_finding_to_dict_shape():
    f = Finding(rule_id="X", title="t", category=Category.SECURITY, severity=Severity.LOW, file="a.py", line=1)
    d = f.to_dict()
    assert d["rule_id"] == "X"
    assert d["severity"] == "low"
    assert d["source"] == "rule"
    assert d["actionable"] is True


# ---------------------------------------------------------------- ReviewResult
def build_result() -> ReviewResult:
    r = ReviewResult()
    r.findings = [
        Finding(rule_id="L", title="low", category=Category.CONVENTION, severity=Severity.LOW, file="b.py", line=2),
        Finding(rule_id="H", title="high", category=Category.SECURITY, severity=Severity.HIGH, file="a.py", line=5),
        Finding(rule_id="M", title="mid", category=Category.PERFORMANCE, severity=Severity.MEDIUM, file="a.py", line=None, anchors_ok=False),
    ]
    return r


def test_result_sort_puts_severe_first_then_by_file_line():
    r = build_result().sort()
    # H 高危排最前；M 为中危（即使无行号也应排 L 之前）
    assert [f.rule_id for f in r.findings] == ["H", "M", "L"]


def test_result_actionable_filters_unanchored():
    r = build_result()
    assert {f.rule_id for f in r.actionable()} == {"H", "L"}


def test_result_by_category_groups():
    r = build_result()
    grouped = r.by_category()
    assert len(grouped["security"]) == 1
    assert len(grouped["performance"]) == 1
    assert len(grouped["convention"]) == 1


def test_result_count_by_severity():
    c = build_result().count_by_severity()
    assert c == {"high": 1, "medium": 1, "low": 1, "info": 0}


def test_result_should_block_only_on_actionable_high():
    r = build_result()
    assert r.should_block() is True

    r2 = ReviewResult()
    r2.findings = [
        Finding(rule_id="H", title="h", category=Category.SECURITY, severity=Severity.HIGH, file="a.py", line=None, anchors_ok=False)
    ]
    assert r2.should_block() is False


# ---------------------------------------------------------------- ReviewStats
def test_stats_total_tokens():
    s = ReviewStats(prompt_tokens=100, completion_tokens=40)
    assert s.total_tokens == 140


def test_stats_to_dict_has_all_fields():
    d = ReviewStats().to_dict()
    for k in ("files", "added_lines", "shards", "rule_findings", "llm_findings",
              "dropped_unanchored", "merged_duplicates", "total_tokens"):
        assert k in d


def test_source_kind_values():
    assert SourceKind.RULE.value == "rule"
    assert SourceKind.LLM.value == "llm"
