"""审查编排测试：分片、行号锚定闸门、合并去重、封顶。"""

import json

import pytest

from pagent.config import Settings
from pagent.llm import LLMError
from pagent.models import Category, Severity, SourceKind
from pagent.retrieval import ConventionStore
from pagent.reviewer import (
    Reviewer,
    ReviewerConfig,
    Shard,
    SYSTEM_PROMPT,
    _skip_file,
    build_shards,
    review_diff_text,
)
from pagent.diffparse import parse_unified_diff


def mk(path: str, *added: str, ctx: int = 2) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{ctx} +1,{ctx+len(added)} @@\n"
        + " c1\n" * 1
        + "".join(f"+{x}\n" for x in added)
    )


class ScriptedLLM:
    """按脚本返回固定 JSON，用来精确测试锚定闸门。"""

    def __init__(self, payload, raise_error=None):
        self.payload = payload
        self.raise_error = raise_error
        self.calls: list[list[dict]] = []

    def chat(self, messages, json_mode=False, temperature=None):
        self.calls.append(messages)
        if self.raise_error:
            raise self.raise_error
        from pagent.llm import LLMResponse

        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return LLMResponse(text=text, prompt_tokens=11, completion_tokens=7)


def settings(**kw) -> Settings:
    base = dict(mock=False, min_severity="low", max_findings_per_file=6, max_files=60, shard_max_chars=6000)
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- 文件筛选
def test_skip_binary_and_deleted_and_empty():
    patch = (
        "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1,1 +1,1 @@\n-same\n+same\n"
    )
    files = {f.path: f for f in parse_unified_diff(patch)}
    assert _skip_file(files["a.png"]) == "binary"
    assert _skip_file(files["b.py"]) == "deleted"
    assert _skip_file(files["c.py"]) is None  # 有 + 行但内容相同，仍需审查


def test_skip_vendored_and_lockfiles():
    for p, expect in [
        ("node_modules/x/index.js", "vendored"),
        ("vendor/lib/a.go", "vendored"),
        ("dist/bundle.js", "vendored"),
        ("frontend/package-lock.json", "vendored"),
        ("assets/logo.svg", "generated-or-binary-suffix"),
        ("a.min.js", "generated-or-binary-suffix"),
    ]:
        assert _skip_file(parse_unified_diff(mk(p, "x = 1"))[0]) == expect


def test_no_added_lines_is_skipped():
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +0,0 @@\n-gone\n"
    # 文件没被删除，但没有任何新增行 —— 无需审查
    assert _skip_file(parse_unified_diff(patch)[0]) == "no-added-lines"


# ---------------------------------------------------------------- 分片
def test_build_shards_one_per_small_file():
    patches = parse_unified_diff(mk("a.py", "x = 1") + mk("b.py", "y = 2"))
    shards = build_shards(patches, max_chars=6000)
    assert [s.file for s in shards] == ["a.py", "b.py"]
    assert all(not s.truncated for s in shards)


def test_build_shards_splits_long_file():
    added = [f"line_{i} = {i}" for i in range(300)]
    files = parse_unified_diff(mk("big.py", *added))
    shards = build_shards(files, max_chars=1500)
    assert len(shards) >= 2
    assert all(s.file == "big.py" for s in shards)
    all_lines = [n for s in shards for n in s.line_numbers]
    assert len(set(all_lines)) == len(all_lines)  # 不重复


def test_shard_carries_line_numbers():
    files = parse_unified_diff(mk("a.py", "x = 1", "y = 2"))
    shard = build_shards(files)[0]
    assert len(shard.line_numbers) == 2
    assert shard.size == len(shard.text)


# ---------------------------------------------------------------- 锚定闸门
def test_hallucinated_line_is_dropped():
    diff = mk("a.py", "x = 1", "y = 2")
    llm = ScriptedLLM({"findings": [{"line": 9999, "title": "编造的问题", "category": "security"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.dropped_unanchored == 1
    assert not [f for f in res.findings if f.source is SourceKind.LLM]


def test_missing_line_field_is_dropped():
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": [{"title": "没有行号", "category": "security"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.dropped_unanchored == 1


def test_valid_line_is_kept():
    diff = mk("a.py", "x = 1", "y = 2")
    llm = ScriptedLLM({"findings": [{"line": 2, "title": "缺事务", "category": "convention", "severity": "medium"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    llm_findings = [f for f in res.findings if f.source is SourceKind.LLM]
    assert len(llm_findings) == 1
    assert llm_findings[0].line == 2
    assert res.stats.dropped_unanchored == 0


def test_model_file_path_is_ignored():
    """模型给的文件名不可信，一律以分片所属文件为准。"""
    diff = mk("real/path.py", "x = 1")
    llm = ScriptedLLM({"findings": [{"line": 2, "title": "t", "file": "attacker/injected.py"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.findings[0].file == "real/path.py"


def test_line_as_string_is_coerced():
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": [{"line": "2", "title": "t"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.findings and res.findings[0].line == 2


def test_invalid_category_and_severity_fall_back():
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": [{"line": 2, "title": "t", "category": "nonsense", "severity": "catastrophic"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    f = rv.review(diff).findings[0]
    assert f.category is Category.CONVENTION
    assert f.severity is Severity.LOW


def test_bad_json_does_not_crash_review():
    diff = mk("a.py", "x = 1")
    rv = Reviewer(settings=settings(), llm=ScriptedLLM("这不是 JSON"), conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.llm_findings == 0


def test_llm_error_is_recorded_not_raised():
    diff = mk("a.py", "x = 1")
    rv = Reviewer(settings=settings(), llm=ScriptedLLM(None, raise_error=LLMError("boom")), conventions=ConventionStore())
    res = rv.review(diff)
    assert any("boom" in e for e in res.errors)


# ---------------------------------------------------------------- 合并去重
def test_rule_and_llm_on_same_line_are_merged_as_corroborated():
    diff = mk("a.py", 'cur.execute("SELECT * FROM u WHERE n = \'" + n + "\'")')
    llm = ScriptedLLM({"findings": [{"line": 2, "title": "SQL 注入风险", "category": "security", "severity": "high"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.merged_duplicates == 1
    target = [f for f in res.findings if f.line == 2]
    assert len(target) == 1
    assert target[0].extra.get("corroborated") is True
    assert target[0].source is SourceKind.RULE  # 规则证据更硬，保留规则那条


def test_merge_records_alternative_titles():
    diff = mk("a.py", "os.system(cmd)")
    llm = ScriptedLLM({"findings": [{"line": 2, "title": "命令注入", "category": "security", "severity": "high"}]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    f = [x for x in rv.review(diff).findings if x.line == 2][0]
    assert f.extra.get("corroborated")
    assert f.extra.get("alt_titles")


def test_merge_counts_only_dropped_duplicates():
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": [
        {"line": 2, "title": "A", "category": "convention"},
        {"line": 2, "title": "B", "category": "convention"},
    ]})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.merged_duplicates == 1
    assert len([f for f in res.findings if f.line == 2]) == 1


# ---------------------------------------------------------------- 过滤与封顶
def test_min_severity_filters_low_findings():
    diff = mk("a.py", 'print("debug")')  # CONV303 低危
    rv = Reviewer(settings=settings(min_severity="high"), llm=None, conventions=ConventionStore())
    assert rv.review(diff).findings == []


def test_cap_per_file_keeps_most_severe():
    diff = mk("a.py", 'print("a")', 'print("b")', 'print("c")', "os.system(x)", "os.system(y)")
    cfg = ReviewerConfig(max_findings_per_file=2)
    rv = Reviewer(settings=settings(), llm=None, config=cfg, conventions=ConventionStore())
    res = rv.review(diff)
    assert len(res.findings) <= 2
    assert res.findings[0].severity is Severity.HIGH


def test_max_files_limit():
    diff = "".join(mk(f"f{i}.py", "x = 1") for i in range(10))
    cfg = ReviewerConfig(max_files=3, max_findings_per_file=6)
    rv = Reviewer(settings=settings(), llm=None, config=cfg, conventions=ConventionStore())
    assert rv.review(diff).stats.files == 3


# ---------------------------------------------------------------- 规范注入
def test_conventions_are_injected_into_prompt():
    store = ConventionStore()
    store.add_text("# 项目规范\n\n禁止裸 except，必须记录日志。这是一段足够长的说明。", source="conv.md")
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": []})
    rv = Reviewer(settings=settings(), llm=llm, conventions=store)
    rv.review(diff, pr_title="异常处理")
    user_msg = [m for m in llm.calls[0] if m["role"] == "user"][0]["content"]
    assert "仓库规范" in user_msg


def test_prompt_states_no_conventions_when_store_empty():
    diff = mk("a.py", "x = 1")
    llm = ScriptedLLM({"findings": []})
    rv = Reviewer(settings=settings(), llm=llm, conventions=ConventionStore())
    rv.review(diff)
    user_msg = [m for m in llm.calls[0] if m["role"] == "user"][0]["content"]
    assert "未提供" in user_msg


def test_system_prompt_requires_line_numbers():
    assert "必须给出行号" in SYSTEM_PROMPT
    assert "无证据不评论" in SYSTEM_PROMPT


# ---------------------------------------------------------------- 统计与结论
def test_stats_accumulate_tokens():
    diff = mk("a.py", "x = 1")
    rv = Reviewer(settings=settings(), llm=ScriptedLLM({"findings": []}), conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.llm_calls == 1
    assert res.stats.total_tokens == 18


def test_should_block_true_on_high_severity():
    diff = mk("a.py", "os.system(cmd)")
    res = Reviewer(settings=settings(), llm=None, conventions=ConventionStore()).review(diff)
    assert res.should_block() is True


def test_should_block_false_after_severity_floor():
    diff = mk("a.py", 'print("debug")')
    res = Reviewer(settings=settings(min_severity="high"), llm=None, conventions=ConventionStore()).review(diff)
    assert res.should_block() is False


def test_empty_diff_reports_error():
    res = Reviewer(settings=settings(), llm=None, conventions=ConventionStore()).review("")
    assert res.errors


def test_disabled_reviewer_runs_rules_only():
    diff = mk("a.py", "os.system(cmd)")
    rv = Reviewer(settings=settings(), llm=ScriptedLLM({"findings": []}), enabled=False, conventions=ConventionStore())
    res = rv.review(diff)
    assert res.stats.llm_calls == 0
    assert res.stats.rule_findings >= 1


def test_findings_sorted_by_severity():
    diff = mk("a.py", 'print("x")', "os.system(y)")
    res = Reviewer(settings=settings(), llm=None, conventions=ConventionStore()).review(diff)
    ranks = [f.severity.rank for f in res.findings]
    assert ranks == sorted(ranks)


def test_review_diff_text_helper_in_mock_mode():
    diff = mk("a.py", "def process(order_id, amount):", "    repo.save(amount)", "    ledger.insert(amount)")
    res = review_diff_text(diff, settings=settings(mock=True), mock=True)
    assert res.stats.llm_calls >= 1
