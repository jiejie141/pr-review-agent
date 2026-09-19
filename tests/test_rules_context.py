"""跨行模式的锚定与去重回归测试。

这两个行为都是先在实际演示里暴露出来、再回头补的测试：

1. **重复报同一处缺陷** —— 循环体内每一行的上下文窗口都包含那个 `for`，
   于是同一个 N+1 被报了 5 次。修法是把锚点定在匹配起始行。
2. **锚点必须落在真实新增行上** —— 否则 GitHub 侧的行内评论会挂到不存在的行。
"""

from pagent.diffparse import parse_unified_diff
from pagent.models import LineKind
from pagent.rules import RuleEngine


def mk_loop(path: str = "app/x.py", body_lines: int = 3, indent: str = "    ") -> str:
    """构造一个「循环内查库」的新增块。"""
    added = ["def load(user_ids):", f"{indent}rows = []", f"{indent}for uid in user_ids:"]
    for i in range(body_lines):
        added.append(f"{indent * 2}rows.append(repo.query(User).get(uid))" if i == 0 else f"{indent * 2}rows.append(uid)")
    added.append(f"{indent}return rows")
    body = "".join(f"+{a}\n" for a in added)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{1 + len(added)} @@\n ctx\n{body}"


def test_context_pattern_reported_once_per_loop():
    diff = mk_loop(body_lines=4)
    rep = RuleEngine().analyze(parse_unified_diff(diff))
    hits = [f for f in rep.findings if f.rule_id == "PERF201"]
    assert len(hits) == 1, f"同一个 N+1 循环应只报一次，实际报了 {len(hits)} 次"


def test_context_pattern_anchor_is_the_for_line():
    diff = mk_loop(body_lines=4)
    rep = RuleEngine().analyze(parse_unified_diff(diff))
    hit = [f for f in rep.findings if f.rule_id == "PERF201"][0]
    # 新增块：第 1 行 ctx，第 2 行 def，第 3 行 rows=[]，第 4 行 for
    assert hit.line == 4
    files = parse_unified_diff(diff)
    anchored = [ln for ln in files[0].added_lines() if ln.new_no == hit.line]
    assert anchored and anchored[0].text.strip().startswith("for ")


def test_two_separate_loops_reported_separately():
    added = [
        "def a(ids):",
        "    rows = []",
        "    for uid in ids:",
        "        rows.append(repo.query(User).get(uid))",
        "    return rows",
        "",
        "",
        "def b(ids):",
        "    rows = []",
        "    for uid in ids:",
        "        rows.append(repo.query(User).get(uid))",
        "    return rows",
    ]
    body = "".join(f"+{a}\n" for a in added)
    diff = f"--- a/app/x.py\n+++ b/app/x.py\n@@ -1,1 +1,{1 + len(added)} @@\n ctx\n{body}"
    rep = RuleEngine().analyze(parse_unified_diff(diff))
    hits = [f for f in rep.findings if f.rule_id == "PERF201"]
    assert len(hits) == 2, "两处独立的 N+1 应分别报出"
    assert hits[0].line != hits[1].line


def test_anchor_line_always_exists_in_added_set():
    """锚定行必须是 diff 里真实存在的新增行，否则评论发不出去。"""
    diff = mk_loop(body_lines=5)
    files = parse_unified_diff(diff)
    valid = {ln.new_no for ln in files[0].added_lines() if ln.new_no is not None}
    for f in RuleEngine().analyze(files).findings:
        assert f.line in valid, f"{f.rule_id} 锚定到 {f.line}，但该行不在新增行集合中"


def test_single_line_pattern_anchor_unaffected():
    """单行模式的锚点仍应是当行，不能被跨行逻辑带偏。"""
    added = ["def f():", "    rows = []", "    return rows", "    os.system(cmd)"]
    body = "".join(f"+{a}\n" for a in added)
    diff = f"--- a/app/x.py\n+++ b/app/x.py\n@@ -1,1 +1,{1 + len(added)} @@\n ctx\n{body}"
    rep = RuleEngine().analyze(parse_unified_diff(diff))
    hit = [f for f in rep.findings if f.rule_id == "SEC102"][0]
    assert hit.line == 5


def test_context_slice_returns_base_index():
    diff = mk_loop(body_lines=3)
    f = parse_unified_diff(diff)[0]
    flat = f.flat_lines()
    base, window = f.context_slice(4, radius=2, flat=flat)
    assert window
    assert flat[base + window[: window.find("for ")].count("\n")].text.strip().startswith("for ")


def test_flat_lines_excludes_no_newline_marker():
    patch = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n+new\n"
    f = parse_unified_diff(patch)[0]
    kinds = [ln.kind for ln in f.flat_lines()]
    assert LineKind.NO_NEWLINE in kinds  # 列表里保留，但分片/规则不应使用它
    assert all(ln.new_no is None or ln.kind is not LineKind.NO_NEWLINE for ln in f.flat_lines())


def test_evidence_still_uses_match_text():
    diff = mk_loop(body_lines=2)
    rep = RuleEngine().analyze(parse_unified_diff(diff))
    hit = [f for f in rep.findings if f.rule_id == "PERF201"][0]
    assert "for uid in user_ids" in hit.evidence
