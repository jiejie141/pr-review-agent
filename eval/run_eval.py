"""评测脚本。

四个指标，分别回答四个不同的问题：

| 指标 | 回答的问题 |
| --- | --- |
| 规则召回率 | 标注好的缺陷，规则库能抓到多少？ |
| 负数误报 | 干净的改动上，会不会无中生有？ |
| 锚定有效率 | 模型给的行号有多少是真实存在的？（幻觉率） |
| 语义命中率 | 正则抓不到的语义缺陷，模型能不能理解？ |

刻意把「规则」和「模型」分开算 —— 混在一起算出来的数字没法指导改进：
分不清是规则库该补，还是提示词该改。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pagent.config import get_settings  # noqa: E402
from pagent.console import ensure_utf8_stdio  # noqa: E402
from pagent.llm import LLMClient, MockLLMClient  # noqa: E402
from pagent.reviewer import Reviewer  # noqa: E402
from pagent.rules import RuleEngine  # noqa: E402
from pagent.diffparse import parse_unified_diff  # noqa: E402


def load_labels() -> dict:
    p = ROOT / "eval" / "labels.json"
    if not p.is_file():
        raise SystemExit("缺少 eval/labels.json，先运行：python eval/build_corpus.py")
    return json.loads(p.read_text(encoding="utf-8"))


def run_case(case: dict, mode: str, settings) -> dict:
    patch = (ROOT / case["patch"]).read_text(encoding="utf-8")

    # --- 规则层 ---
    files = parse_unified_diff(patch)
    rule_report = RuleEngine().analyze(files)
    hit_rules = sorted({f.rule_id for f in rule_report.findings})
    expected = set(case["expect_rules"])
    hit_expected = sorted(expected & set(hit_rules))
    missed = sorted(expected - set(hit_rules))
    extra = sorted(set(hit_rules) - expected)

    # --- 模型层 ---
    llm_findings: list[dict] = []
    dropped = 0
    if mode != "rules":
        llm = MockLLMClient() if mode == "mock" else None
        if mode == "real":
            if not settings.llm_ready:
                raise SystemExit("real 模式需要 LLM_API_KEY")
            llm = LLMClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout=settings.llm_timeout,
                max_retries=settings.llm_max_retries,
                temperature=settings.llm_temperature,
            )
        rv = Reviewer(settings=settings, llm=llm, enabled=True)
        result = rv.review(patch, pr_title=case["title"])
        llm_findings = [f.to_dict() for f in result.findings if f.source.value == "llm"]
        dropped = result.stats.dropped_unanchored
        merged = result.stats.merged_duplicates
    else:
        merged = 0

    semantic_expected = case.get("expect_semantic") or []
    semantic_hit = len(llm_findings) > 0 if semantic_expected else None

    return {
        "id": case["id"],
        "title": case["title"],
        "negative": case["negative"],
        "heldout": case.get("heldout", False),
        "expected_rules": sorted(expected),
        "hit_rules": hit_expected,
        "missed_rules": missed,
        "extra_rules": extra,
        "rule_findings": len(rule_report.findings),
        "llm_findings": len(llm_findings),
        "dropped_unanchored": dropped,
        "merged_duplicates": merged,
        "semantic_expected": semantic_expected,
        "semantic_hit": semantic_hit,
        "llm_detail": [
            {"line": f.get("line"), "title": f.get("title"), "category": f.get("category")}
            for f in llm_findings
        ],
    }


def _recall(rows: list[dict]) -> tuple[int, int, float]:
    exp = sum(len(r["expected_rules"]) for r in rows)
    hit = sum(len(r["hit_rules"]) for r in rows)
    return hit, exp, (hit / exp if exp else 0.0)


def summarize(results: list[dict], mode: str) -> dict:
    positives = [r for r in results if not r["negative"]]
    negatives = [r for r in results if r["negative"]]

    tuned = [r for r in positives if not r["heldout"]]
    held = [r for r in positives if r["heldout"]]
    t_hit, t_exp, t_rec = _recall(tuned)
    h_hit, h_exp, h_rec = _recall(held)
    exp_total, hit_total = t_exp + h_exp, t_hit + h_hit
    rule_recall = hit_total / exp_total if exp_total else 0.0

    fp_findings = sum(r["rule_findings"] + r["llm_findings"] for r in negatives)
    fp_cases = sum(1 for r in negatives if (r["rule_findings"] + r["llm_findings"]) > 0)
    # 正例上「期望之外还报了什么」—— 过度报告同样是误报
    over_report = sum(len(r["extra_rules"]) for r in positives)

    kept = sum(r["llm_findings"] for r in results)
    dropped = sum(r["dropped_unanchored"] for r in results)
    anchor_rate = kept / (kept + dropped) if (kept + dropped) else 1.0

    sem_cases = [r for r in positives if r["semantic_expected"]]
    sem_hit = sum(1 for r in sem_cases if r["semantic_hit"])

    return {
        "mode": mode,
        "cases": len(results),
        "positive_cases": len(positives),
        "negative_cases": len(negatives),
        "expected_rule_hits": exp_total,
        "actual_rule_hits": hit_total,
        "rule_recall": round(rule_recall, 4),
        "tuned_cases": len(tuned),
        "tuned_expected": t_exp,
        "tuned_hits": t_hit,
        "tuned_recall": round(t_rec, 4),
        "heldout_cases": len(held),
        "heldout_expected": h_exp,
        "heldout_hits": h_hit,
        "heldout_recall": round(h_rec, 4),
        "rule_misses": sorted({m for r in positives for m in r["missed_rules"]}),
        "over_reported_rules": sorted({e for r in positives for e in r["extra_rules"]}),
        "over_report_count": over_report,
        "false_positive_findings": fp_findings,
        "false_positive_cases": fp_cases,
        "negative_case_count": len(negatives),
        "llm_findings_kept": kept,
        "llm_findings_dropped": dropped,
        "anchor_valid_rate": round(anchor_rate, 4),
        "semantic_cases": len(sem_cases),
        "semantic_hits": sem_hit,
        "semantic_rate": round(sem_hit / len(sem_cases), 4) if sem_cases else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def print_report(results: list[dict], s: dict) -> None:
    print("")
    print("=" * 78)
    print(f"  PR 审查 Agent 评测报告        模式：{s['mode']}")
    print("=" * 78)
    print("")
    print("【逐用例明细】")
    print(f"{'用例':<24}{'期望':>5}{'命中':>5}{'漏报':>5}{'规则':>5}{'模型':>5}{'弃用':>5}  语义")
    print("-" * 78)
    for r in results:
        sem = "-" if r["semantic_hit"] is None else ("✓" if r["semantic_hit"] else "✗")
        tag = " [负例]" if r["negative"] else (" [留出]" if r["heldout"] else "")
        print(
            f"{r['id']:<24}{len(r['expected_rules']):>5}{len(r['hit_rules']):>5}"
            f"{len(r['missed_rules']):>5}{r['rule_findings']:>5}{r['llm_findings']:>5}"
            f"{r['dropped_unanchored']:>5}   {sem}{tag}"
        )
    print("")
    print("【汇总指标】")
    print(
        f"  规则召回（合计）: {s['actual_rule_hits']}/{s['expected_rule_hits']} = {s['rule_recall']*100:.1f}%"
    )
    print(
        f"    · 调参集      : {s['tuned_hits']}/{s['tuned_expected']} = {s['tuned_recall']*100:.1f}%"
        f"（{s['tuned_cases']} 个，模式库是照着它调的，参考价值有限）"
    )
    print(
        f"    · 留出集      : {s['heldout_hits']}/{s['heldout_expected']} = {s['heldout_recall']*100:.1f}%"
        f"（{s['heldout_cases']} 个，**这才是可对外说的数字**）"
    )
    if s["rule_misses"]:
        print(f"  漏报的规则      : {', '.join(s['rule_misses'])}")
    if s["over_reported_rules"]:
        print(
            f"  未标注的命中    : {', '.join(s['over_reported_rules'])}（共 {s['over_report_count']} 处）"
            " —— 可能是漏标的真实缺陷，也可能是误报，需人工确认"
        )
    print(
        f"  负数误报        : {s['false_positive_findings']} 条意见 / {s['negative_case_count']} 个干净用例"
        f"（{s['false_positive_cases']} 个用例被误报）"
    )
    print(
        f"  锚定有效率      : {s['llm_findings_kept']}/{s['llm_findings_kept']+s['llm_findings_dropped']}"
        f" = {s['anchor_valid_rate']*100:.1f}%（被丢弃的都是行号不存在的幻觉）"
    )
    if s["semantic_rate"] is not None:
        print(f"  语义缺陷命中率  : {s['semantic_hits']}/{s['semantic_cases']} = {s['semantic_rate']*100:.1f}%")
    print("")
    print("【怎么读这些数字】")
    print("  · **只看留出集**。调参集上的高分是自己出题自己答，没有信息量。")
    print("  · 负数误报必须是 0 —— 干净代码上提意见会直接损害工具的可信度。")
    print("  · 锚定有效率反映提示词对行号的约束力；低于 90% 说明该收紧提示词了。")
    print("  · 语义命中率是模型相对规则的增量价值，这部分规则永远做不到。")
    print("=" * 78)


def main() -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="PR 审查 Agent 评测")
    ap.add_argument("--mode", choices=["rules", "mock", "real"], default="mock",
                    help="rules=只跑规则库；mock=规则+离线替身；real=规则+真实模型")
    ap.add_argument("--out", default=str(ROOT / "runs"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    labels = load_labels()
    settings = get_settings()
    if args.mode == "real":
        settings.mock = False

    results = [run_case(c, args.mode, settings) for c in labels["cases"]]
    s = summarize(results, args.mode)
    print_report(results, s)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = out / f"eval_{args.mode}_{ts}.json"
    p.write_text(
        json.dumps({"summary": s, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已写入：{p}")
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
