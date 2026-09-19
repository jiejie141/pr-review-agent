"""报告渲染测试。"""

import json

from pagent.models import (
    Category,
    Finding,
    ReviewResult,
    ReviewStats,
    Severity,
    SourceKind,
)
from pagent.report import (
    render_console,
    render_markdown,
    render_pr_comment,
    summary_line,
    to_json,
    write_reports,
)


def sample_result(blocking: bool = True) -> ReviewResult:
    r = ReviewResult(pr_title="#42 修复登录", pr_url="https://github.com/o/r/pull/42")
    r.stats = ReviewStats(
        files=3,
        added_lines=57,
        shards=3,
        rule_findings=2,
        llm_findings=1,
        dropped_unanchored=2,
        merged_duplicates=1,
        prompt_tokens=1200,
        completion_tokens=300,
        llm_calls=3,
    )
    r.findings = [
        Finding(
            rule_id="SEC101",
            title="SQL 注入：动态拼接查询语句",
            category=Category.SECURITY,
            severity=Severity.HIGH if blocking else Severity.LOW,
            file="app/db.py",
            line=12,
            evidence='execute("... = \'" + name + "\'")',
            suggestion="改用参数化查询。",
            confidence=0.85,
            source=SourceKind.RULE,
        ),
        Finding(
            rule_id="LLM-CON",
            title="事务边界缺失",
            category=Category.CONVENTION,
            severity=Severity.MEDIUM,
            file="app/db.py",
            line=20,
            evidence="repo.save(order)",
            suggestion="包进同一个事务。",
            confidence=0.6,
            source=SourceKind.LLM,
            extra={"corroborated": True},
        ),
        Finding(
            rule_id="LLM-PER",
            title="没有行号的意见",
            category=Category.PERFORMANCE,
            severity=Severity.LOW,
            file="app/db.py",
            line=None,
            source=SourceKind.LLM,
            anchors_ok=False,
        ),
    ]
    return r.sort()


def test_summary_line_contains_verdict_and_counts():
    line = summary_line(sample_result(True))
    assert "阻断合入" in line
    assert "高 1" in line
    assert "token" in line


def test_summary_line_says_pass_when_not_blocking():
    assert "通过" in summary_line(sample_result(False))


def test_markdown_has_all_sections():
    md = render_markdown(sample_result())
    for section in ("# PR 代码审查报告", "## 结论", "## 审查过程", "## 缺陷类型分布", "## 审查口径"):
        assert section in md


def test_markdown_lists_severity_table():
    md = render_markdown(sample_result())
    assert "🔴 高" in md
    assert "| **合计** | **3** |" in md


def test_markdown_explains_dropped_unanchored():
    md = render_markdown(sample_result())
    assert "行号在 diff 中不存在被丢弃" in md


def test_markdown_groups_by_category():
    md = render_markdown(sample_result())
    assert "## 安全（1 条）" in md
    assert "## 规范（1 条）" in md


def test_markdown_handles_no_findings():
    r = ReviewResult()
    md = render_markdown(r)
    assert "## 未发现问题" in md
    assert "未发现阻断性问题" in md


def test_markdown_includes_errors_section():
    r = ReviewResult()
    r.errors.append("[a.py] 模型调用失败：超时")
    assert "## 执行异常" in render_markdown(r)


def test_markdown_respects_max_per_section():
    r = ReviewResult()
    r.findings = [
        Finding(
            rule_id="X",
            title=f"问题 {i}",
            category=Category.SECURITY,
            severity=Severity.LOW,
            file="a.py",
            line=i,
        )
        for i in range(1, 51)
    ]
    md = render_markdown(r, max_per_section=5)
    assert "另有 45 条" in md


def test_pr_comment_is_compact_and_marked():
    c = render_pr_comment(sample_result())
    assert "🤖 自动代码审查" in c
    assert "建议修复后再合入" in c
    assert "🔁" in c  # 双通道一致标记
    assert "<details" in c


def test_pr_comment_notes_dropped_count():
    assert "防幻觉" in render_pr_comment(sample_result())


def test_pr_comment_truncates():
    r = ReviewResult()
    r.findings = [
        Finding(rule_id="X", title=f"t{i}", category=Category.SECURITY, severity=Severity.LOW, file="a.py", line=i)
        for i in range(30)
    ]
    c = render_pr_comment(r, max_items=3)
    assert "另有 27 条" in c


def test_json_export_shape_and_validity():
    payload = json.loads(to_json(sample_result()))
    assert payload["verdict"]["block"] is True
    assert payload["verdict"]["total"] == 3
    assert payload["stats"]["total_tokens"] == 1500
    assert len(payload["findings"]) == 3
    assert payload["findings"][0]["rule_id"] == "SEC101"
    assert payload["findings"][2]["actionable"] is False


def test_console_render_contains_key_fields():
    out = render_console(sample_result())
    assert "app/db.py:12" in out
    assert "SEC101" in out
    assert "证据:" in out


def test_console_render_marks_corroborated():
    assert "🔁" in render_console(sample_result())


def test_console_render_handles_empty():
    assert "未发现问题" in render_console(ReviewResult())


def test_write_reports_creates_three_files(tmp_path):
    paths = write_reports(sample_result(), str(tmp_path), stem="pr42")
    for key in ("markdown", "json", "comment"):
        assert key in paths
    from pathlib import Path

    for p in paths.values():
        assert Path(p).is_file()
        assert Path(p).read_text(encoding="utf-8").strip()
    assert paths["markdown"].endswith("pr42.md")
