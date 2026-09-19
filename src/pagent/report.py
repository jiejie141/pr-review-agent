"""报告渲染：Markdown / JSON / CI 摘要。"""

from __future__ import annotations

import json
from datetime import datetime

from .models import Finding, ReviewResult, Severity
from .rules import summarize_by_rule

SEV_LABEL = {
    "high": "🔴 高",
    "medium": "🟠 中",
    "low": "🟡 低",
    "info": "⚪ 提示",
}
CAT_LABEL = {
    "security": "安全",
    "performance": "性能",
    "convention": "规范",
}


def summary_line(result: ReviewResult) -> str:
    """一行式结论，给 CI 日志用。"""
    c = result.count_by_severity()
    total = len(result.findings)
    verdict = "阻断合入" if result.should_block() else "通过"
    return (
        f"[pr-review] {verdict} | 共 {total} 条意见 "
        f"(高 {c['high']} / 中 {c['medium']} / 低 {c['low']}) | "
        f"文件 {result.stats.files} 个 / 新增行 {result.stats.added_lines} 行 | "
        f"模型调用 {result.stats.llm_calls} 次 / token {result.stats.total_tokens}"
    )


def render_markdown(result: ReviewResult, max_per_section: int = 40) -> str:
    """完整报告。这是归档和复盘看的版本。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = result.count_by_severity()
    out: list[str] = []

    out.append("# PR 代码审查报告")
    out.append("")
    if result.pr_title:
        out.append(f"**PR**：{result.pr_title}")
    if result.pr_url:
        out.append(f"**链接**：{result.pr_url}")
    out.append(f"**生成时间**：{now}")
    out.append("")

    # --- 结论 ---
    verdict = "🚫 **建议阻断合入**（存在高严重级别问题）" if result.should_block() else "✅ **未发现阻断性问题**"
    out.append("## 结论")
    out.append("")
    out.append(verdict)
    out.append("")
    out.append("| 严重级别 | 数量 |")
    out.append("| --- | --- |")
    for s in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        out.append(f"| {SEV_LABEL[s.value]} | {c[s.value]} |")
    out.append(f"| **合计** | **{len(result.findings)}** |")
    out.append("")

    # --- 审查过程指标 ---
    st = result.stats
    out.append("## 审查过程")
    out.append("")
    out.append("| 指标 | 数值 |")
    out.append("| --- | --- |")
    out.append(f"| 审查文件数 | {st.files} |")
    out.append(f"| 新增代码行 | {st.added_lines} |")
    out.append(f"| 分片数 | {st.shards} |")
    out.append(f"| 规则命中 | {st.rule_findings} |")
    out.append(f"| 模型命中 | {st.llm_findings} |")
    out.append(f"| 因缺少有效行号被丢弃 | {st.dropped_unanchored} |")
    out.append(f"| 合并重复意见 | {st.merged_duplicates} |")
    out.append(f"| 模型调用次数 | {st.llm_calls} |")
    out.append(f"| token 消耗 | {st.total_tokens}（输入 {st.prompt_tokens} / 输出 {st.completion_tokens}）|")
    out.append("")

    if st.dropped_unanchored:
        out.append(
            f"> 有 **{st.dropped_unanchored}** 条模型意见因行号在 diff 中不存在被丢弃 —— "
            "这类意见属于幻觉，发出去会挂到错误的代码行上。这个数字越小，说明提示词对行号的约束越有效。"
        )
        out.append("")

    # --- 按维度分组 ---
    grouped = result.by_category()
    for cat in ("security", "performance", "convention"):
        items = grouped.get(cat, [])
        if not items:
            continue
        out.append(f"## {CAT_LABEL[cat]}（{len(items)} 条）")
        out.append("")
        for f in items[:max_per_section]:
            out.append(f.render())
            out.append("")
        if len(items) > max_per_section:
            out.append(f"... 另有 {len(items) - max_per_section} 条，见 JSON 报告。")
            out.append("")

    if not result.findings:
        out.append("## 未发现问题")
        out.append("")
        out.append("规则库与模型均未在本次新增代码中发现需要指出的问题。")
        out.append("")

    # --- 规则命中排行 ---
    rows = summarize_by_rule(result.findings)
    if rows:
        out.append("## 缺陷类型分布")
        out.append("")
        out.append("| 规则 | 类型 | 维度 | 严重级别 | 命中 |")
        out.append("| --- | --- | --- | --- | --- |")
        for rid, title, cat, sev, n in rows:
            out.append(
                f"| `{rid}` | {title} | {CAT_LABEL[cat.value]} | {SEV_LABEL[sev.value]} | {n} |"
            )
        out.append("")

    out.append("## 审查口径")
    out.append("")
    out.append("- **只审查新增与修改的行**，不评论未改动的历史代码。")
    out.append("- **无行号不评论**：模型给出的行号若不在 diff 内，该条意见直接丢弃并计数。")
    out.append("- **无证据不评论**：说不出具体风险的「建议优化」类意见不采纳。")
    out.append("- **规则与模型分别统计**：规则命中可复现且零成本，模型负责语义判断，两者一致性在 JSON 报告里体现。")
    out.append("- **单文件意见数封顶**：避免一份报告堆到没人愿意读。")
    out.append("")

    if result.errors:
        out.append("## 执行异常")
        out.append("")
        for e in result.errors:
            out.append(f"- {e}")
        out.append("")

    return "\n".join(out)


def render_pr_comment(result: ReviewResult, max_items: int = 10) -> str:
    """贴到 PR 上的摘要评论。控制在能一屏读完的长度。"""
    c = result.count_by_severity()
    out: list[str] = []
    head = "## 🤖 自动代码审查"
    out.append(head)
    out.append("")
    if result.should_block():
        out.append("> 🚫 **发现高严重级别问题，建议修复后再合入。**")
    else:
        out.append("> ✅ 未发现阻断性问题。以下是可选改进项。")
    out.append("")
    out.append(
        f"高 **{c['high']}** · 中 **{c['medium']}** · 低 **{c['low']}**  "
        f"｜ 审查 {result.stats.files} 个文件 / {result.stats.added_lines} 行新增代码"
    )
    out.append("")

    if result.findings:
        out.append("<details open>")
        out.append(f"<summary>审查意见（{len(result.findings)} 条）</summary>")
        out.append("")
        for f in result.findings[:max_items]:
            loc = f"{f.file}:{f.line}" if f.line else f.file
            tag = f"{SEV_LABEL[f.severity.value]} · {CAT_LABEL[f.category.value]}"
            flag = " 🔁" if f.extra.get("corroborated") else ""
            out.append(f"- **{f.title}**{flag} — `{loc}` ｜ {tag}")
            if f.suggestion:
                out.append(f"  <br>建议：{f.suggestion}")
        if len(result.findings) > max_items:
            out.append(f"- ... 另有 {len(result.findings) - max_items} 条，详见归档报告。")
        out.append("")
        out.append("</details>")
        out.append("")

    if result.stats.dropped_unanchored:
        out.append(
            f"<sub>另有 {result.stats.dropped_unanchored} 条意见因行号不在 diff 内被自动丢弃（防幻觉）。</sub>"
        )
        out.append("")

    out.append(f"<sub>token 消耗 {result.stats.total_tokens} ｜ 规则 {result.stats.rule_findings} 条 · 模型 {result.stats.llm_findings} 条 🔁 = 双通道一致</sub>")
    return "\n".join(out)


def to_json(result: ReviewResult, indent: int = 2) -> str:
    payload = {
        "pr_title": result.pr_title,
        "pr_url": result.pr_url,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "verdict": {
            "block": result.should_block(),
            "total": len(result.findings),
            "by_severity": result.count_by_severity(),
        },
        "stats": result.stats.to_dict(),
        "findings": [f.to_dict() for f in result.findings],
        "errors": result.errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def render_console(result: ReviewResult) -> str:
    """终端里直接看的紧凑版。"""
    out: list[str] = ["", "=" * 72]
    out.append(summary_line(result))
    out.append("=" * 72)
    if not result.findings:
        out.append("  未发现问题。")
    for f in result.findings:
        loc = f"{f.file}:{f.line}" if f.line else f"{f.file} (无行号)"
        mark = "🔁" if f.extra.get("corroborated") else "  "
        out.append(f"{mark} [{f.severity.value.upper():6}] {f.rule_id:9} {loc}")
        out.append(f"           {f.title}")
        if f.evidence:
            out.append(f"           证据: {f.evidence[:100]}")
        if f.suggestion:
            out.append(f"           建议: {f.suggestion[:120]}")
        out.append("")
    for e in result.errors:
        out.append(f"  ! {e}")
    out.append("=" * 72)
    return "\n".join(out)


def write_reports(result: ReviewResult, out_dir: str, stem: str = "review") -> dict[str, str]:
    """落盘：Markdown + JSON + PR 评论草稿。"""
    from pathlib import Path

    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    md = d / f"{stem}.md"
    js = d / f"{stem}.json"
    cm = d / f"{stem}.comment.md"
    md.write_text(render_markdown(result), encoding="utf-8")
    js.write_text(to_json(result), encoding="utf-8")
    cm.write_text(render_pr_comment(result), encoding="utf-8")
    return {"markdown": str(md), "json": str(js), "comment": str(cm)}
