"""规则引擎测试：注释守卫、扩展名过滤、上下文匹配、去重。"""

from pagent.diffparse import parse_unified_diff
from pagent.models import Category, Severity, SourceKind
from pagent.patterns import get_pattern
from pagent.rules import RuleEngine, comment_start, summarize_by_rule

HEAD = "--- a/{p}\n+++ b/{p}\n@@ -1,2 +1,{n} @@\n x\n y\n"


def mk(path: str, *added: str) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,{2+len(added)} @@\n x\n y\n{body}"


# ---------------------------------------------------------------- 注释守卫
def test_comment_start_detects_python_hash():
    assert comment_start("    # hello", ".py") == 4


def test_comment_start_ignores_hash_inside_string():
    line = 'print("a # b")'
    assert comment_start(line, ".py") is None


def test_comment_start_handles_escaped_quote():
    line = 'x = "he said \\"hi\\" # not comment"'
    assert comment_start(line, ".py") is None


def test_comment_start_for_js_and_sql():
    assert comment_start("  // note", ".js") == 2
    assert comment_start("SELECT 1 -- note", ".sql") == 9


def test_comment_start_unknown_extension_returns_none():
    assert comment_start("whatever # here", ".txt") is None


def test_rules_skip_dangerous_code_in_full_line_comment():
    patch = mk("app/notes.py", "    # 不要用 os.system(cmd)", "    # 也别写 verify=False")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert rep.findings == []


def test_rules_still_fire_on_real_code():
    patch = mk("app/x.py", "    os.system(cmd)")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert [f.rule_id for f in rep.findings] == ["SEC102"]


def test_todo_rule_still_reads_comments():
    """CONV304 显式关闭了注释跳过，注释里的 TODO 必须能抓到。"""
    patch = mk("app/x.py", "    # TODO: 补上校验")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "CONV304" in [f.rule_id for f in rep.findings]


def test_comment_after_code_does_not_suppress_real_hit():
    """代码在前、注释在后的行，命中位置在注释之前，不应被跳过。"""
    patch = mk("app/x.py", '    cur.execute("SELECT * FROM u WHERE n = \'" + n + "\'")  # noqa')
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "SEC101" in [f.rule_id for f in rep.findings]


# ---------------------------------------------------------------- 扩展名过滤
def test_extension_filter_blocks_python_only_rule_in_js():
    """CONV305（可变默认参数）只对 Python 有意义，不应在 .js 上误报。"""
    patch = mk("web/app.js", "function add(tags = []) {")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "CONV305" not in [f.rule_id for f in rep.findings]


def test_extension_filter_allows_python_only_rule_in_python():
    patch = mk("app/x.py", "def add_tag(name, tags=[]):")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "CONV305" in [f.rule_id for f in rep.findings]


def test_file_handle_rule_applies_across_languages():
    """CONV308 覆盖 py/rb 的 open、JS 的 fs.openSync、Java 的 FileInputStream，不限扩展名。"""
    patch = mk("app/x.rb", "    fh = open(path)")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "CONV308" in [f.rule_id for f in rep.findings]


# ---------------------------------------------------------------- 匹配行为
def test_context_pattern_matches_across_lines():
    patch = mk(
        "app/x.py",
        "def load(ids):",
        "    rows = []",
        "    for uid in ids:",
        "        rows.append(repo.query(User).get(uid))",
    )
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "PERF201" in [f.rule_id for f in rep.findings]


def test_single_line_pattern_not_matched_against_window():
    """回归测试：窗口化曾导致带 ^/$ 锚点的规则全部失效。"""
    patch = mk("app/x.py", "    try:", "        pass", "    except:", "        return 0")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert "CONV302" in [f.rule_id for f in rep.findings]


def test_finding_anchors_to_added_line():
    patch = mk("app/x.py", "    return 1", "    os.system(cmd)")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    f = rep.findings[0]
    assert f.line == 4  # 两行上下文占 1~2，return 1 在第 3 行，os.system 在第 4 行
    assert f.file == "app/x.py"
    assert f.source is SourceKind.RULE
    assert f.anchors_ok and f.is_actionable


def test_finding_evidence_is_recorded_and_truncated():
    long_line = "    os.system(" + "a" * 500 + ")"
    patch = mk("app/x.py", long_line)
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert rep.findings[0].evidence
    assert len(rep.findings[0].evidence) <= 240


def test_finding_carries_severity_and_category():
    patch = mk("app/x.py", "    os.system(cmd)")
    f = RuleEngine().analyze(parse_unified_diff(patch)).findings[0]
    assert f.category is Category.SECURITY
    assert f.severity is Severity.HIGH
    assert f.severity.blocks_ci is True


def test_same_rule_same_line_reported_once():
    """一行里出现两次同类模式，只报一条。"""
    patch = mk("app/x.py", "    os.system(cmd); os.system(other)")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    sec102 = [f for f in rep.findings if f.rule_id == "SEC102"]
    assert len(sec102) == 1


# ---------------------------------------------------------------- 引擎配置
def test_disabled_rules_are_skipped():
    patch = mk("app/x.py", "    os.system(cmd)")
    eng = RuleEngine(disabled={"SEC102"})
    assert eng.analyze(parse_unified_diff(patch)).findings == []


def test_min_confidence_filters_low_confidence_rules():
    patch = mk("app/x.py", '    print("debug")')
    assert RuleEngine(min_confidence=0.9).analyze(parse_unified_diff(patch)).findings == []
    assert RuleEngine(min_confidence=0.1).analyze(parse_unified_diff(patch)).findings


def test_binary_and_deleted_files_are_counted_not_analyzed():
    patch = (
        "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    )
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert rep.skipped_binary == 1
    assert rep.skipped_deleted == 1
    assert rep.findings == []


def test_hits_by_rule_counter():
    patch = mk("app/x.py", "    os.system(a)", "    os.system(b)", "    os.system(c)")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert rep.hits_by_rule["SEC102"] == 3


def test_scanned_lines_count():
    patch = mk("app/x.py", "    a = 1", "    b = 2")
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    assert rep.scanned_lines == 2


def test_summarize_by_rule_sorted_by_severity_then_count():
    patch = mk(
        "app/x.py",
        "    os.system(a)",
        "    os.system(b)",
        '    print("x")',
    )
    rep = RuleEngine().analyze(parse_unified_diff(patch))
    rows = summarize_by_rule(rep.findings)
    assert rows[0][0] == "SEC102"
    assert rows[0][4] == 2


def test_pattern_lookup_helper():
    assert get_pattern("SEC101").id == "SEC101"
