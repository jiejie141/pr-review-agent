"""命令行入口。

三档能力，按成本从低到高：
1. `--rules-only`：只跑规则库。零 token、零网络，CI 里当廉价门禁用。
2. `--mock`：规则 + 离线模型替身。跑通全链路但不花钱，用于开发与回归。
3. 默认：规则 + 真实模型。产出最终审查意见。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import PROJECT_ROOT, get_settings
from .console import ensure_utf8_stdio
from .diffparse import parse_unified_diff
from .github import GitHubClient, GitHubError, parse_pr_ref
from .llm import LLMClient, LLMError, MockLLMClient
from .patterns import PATTERNS, catalog, stats_by_category
from .report import render_console, render_markdown, render_pr_comment, summary_line, to_json
from .reviewer import Reviewer, SKIP_PATH_PARTS, SKIP_SUFFIXES, build_shards
from .rules import RuleEngine


def _read_diff(args) -> tuple[str, str, str]:
    """返回 (diff 文本, PR 标题, PR 链接)。"""
    if args.diff:
        if args.diff == "-":
            return sys.stdin.read(), "", ""
        p = Path(args.diff)
        if not p.is_file():
            raise SystemExit(f"找不到 diff 文件：{p}")
        return p.read_text(encoding="utf-8", errors="replace"), "", ""

    if args.local_diff:
        parts = args.local_diff.split("..", 1)
        base = parts[0] or "HEAD~1"
        head = parts[1] if len(parts) > 1 else "HEAD"
        from .github import git_local_diff

        return git_local_diff(base, head), "", ""

    if args.pr:
        st = get_settings()
        ref = parse_pr_ref(args.pr)
        if not st.github_token and not args.mock:
            raise SystemExit(
                "读取 GitHub PR 需要 GITHUB_TOKEN：请在 .env 中配置，"
                "或改用 --diff 传本地 diff 文件。"
            )
        client = GitHubClient(
            token=st.github_token, api_base=st.github_api_base, dry_run=True
        )
        pr = client.get_pr(ref)
        title = f"#{ref.number} {pr.get('title', '')}".strip()
        url = pr.get("html_url", "")
        return client.get_diff(ref), title, url

    raise SystemExit("需要指定一种输入：--diff 文件 / --local-diff base..head / --pr owner/repo#123")


def cmd_doctor(args) -> int:
    st = get_settings(reload=True)
    st.mock = args.mock
    print(st.doctor())
    from .retrieval import build_store

    store = build_store(st.conventions)
    print("")
    print("[规范检索]")
    s = store.stats()
    print(f"  索引块数      : {s['chunks']}")
    print(f"  词表大小      : {s['vocab']}")
    print(f"  平均长度      : {s['avg_tokens']} tokens")
    for src in s["sources"]:
        print(f"    - {src}")
    if s["chunks"] == 0:
        print("  提示：examples/conventions/ 下放一些规范文档，检索侧就有内容可用了。")

    print("")
    print("[规则库]")
    for cat, n in stats_by_category().items():
        print(f"  {cat:12}: {n} 类")
    print(f"  {'合计':12}: {len(PATTERNS)} 类")

    print("")
    print("[分片策略]")
    print(f"  跳过后缀      : {len(SKIP_SUFFIXES)} 种（二进制、压缩包、锁文件等）")
    print(f"  跳过路径      : {len(SKIP_PATH_PARTS)} 种（node_modules、vendor、dist 等）")
    print("")
    print("[LLM 连通性]")
    if args.mock:
        print("  离线替身模式，跳过网络检查。")
    elif not st.llm_ready:
        print("  未配置 API Key，跳过。加 --mock 可离线跑通流程。")
    else:
        try:
            client = LLMClient(
                api_key=st.llm_api_key,
                base_url=st.llm_base_url,
                model=st.llm_model,
                timeout=st.llm_timeout,
                max_retries=1,
            )
            resp = client.chat([{"role": "user", "content": "回复两个字：可用"}], temperature=0)
            print(f"  ✓ 连通成功（{resp.model}）：{resp.text.strip()[:30]}")
        except LLMError as e:
            print(f"  ✗ 调用失败：{e}")
            return 1
    return 0


def cmd_rules(args) -> int:
    rows = catalog()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"缺陷模式库：共 {len(rows)} 类")
    for cat, n in stats_by_category().items():
        print(f"  {cat}: {n} 类")
    print("")
    cur = None
    for r in rows:
        if r["category"] != cur:
            cur = r["category"]
            print(f"--- {cur} ---")
        ext = ",".join(r["extensions"][:4])
        flag = " [需上下文]" if r["needs_context"] else ""
        print(f"  {r['id']}  {r['severity']:<6} {r['title']}{flag}")
        print(f"          {r['description']}")
        print(f"          适用: {ext}  置信度: {r['confidence']}")
    return 0


def cmd_review(args) -> int:
    st = get_settings(reload=True)
    if args.mock:
        st.mock = True
    diff_text, title, url = _read_diff(args)

    files = parse_unified_diff(diff_text)
    if not files:
        print("diff 为空或无法解析。", file=sys.stderr)
        return 2

    # --- 组装 LLM ---
    llm = None
    if args.rules_only:
        pass
    elif st.mock:
        llm = MockLLMClient()
    elif st.llm_ready:
        llm = LLMClient(
            api_key=st.llm_api_key,
            base_url=st.llm_base_url,
            model=st.llm_model,
            timeout=st.llm_timeout,
            max_retries=st.llm_max_retries,
            temperature=st.llm_temperature,
        )
    else:
        print(
            "未配置 LLM_API_KEY，本次只跑规则库。\n"
            "提示：加 --mock 可以用离线替身跑通完整链路；加 --rules-only 明确只跑规则。",
            file=sys.stderr,
        )

    rv = Reviewer(settings=st, llm=llm, enabled=llm is not None)
    result = rv.review(diff_text, pr_title=title, pr_url=url)

    # --- 输出 ---
    print(render_console(result))

    out_dir = Path(args.out) if args.out else None
    if args.json:
        print(to_json(result))

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = args.stem or "review"
        (out_dir / f"{stem}.md").write_text(render_markdown(result), encoding="utf-8")
        (out_dir / f"{stem}.json").write_text(to_json(result), encoding="utf-8")
        (out_dir / f"{stem}.comment.md").write_text(render_pr_comment(result), encoding="utf-8")
        print(f"报告已写入：{out_dir / (stem + '.md')}")
        print(f"            {out_dir / (stem + '.json')}")
        print(f"            {out_dir / (stem + '.comment.md')}")

    # --- 回写 GitHub ---
    if args.post:
        if not args.pr:
            print("--post 需要配合 --pr 使用。", file=sys.stderr)
            return 2
        st2 = get_settings()
        ref = parse_pr_ref(args.pr)
        client = GitHubClient(
            token=st2.github_token,
            api_base=st2.github_api_base,
            dry_run=not args.apply,
        )
        comments = [
            {
                "path": f.file,
                "line": f.line,
                "side": "RIGHT",
                "body": f"**[{f.severity.value}] {f.title}**  `{f.rule_id}`\n\n{f.suggestion}"
                + (f"\n\n> 证据：`{f.evidence}`" if f.evidence else ""),
            }
            for f in result.actionable()
        ]
        event = "REQUEST_CHANGES" if result.should_block() and args.block else "COMMENT"
        try:
            resp = client.post_review(
                ref, render_pr_comment(result), comments, event=event
            )
        except GitHubError as e:
            print(f"回写失败：{e}", file=sys.stderr)
            return 1
        for line in client.log:
            print(line)
        if resp.get("dry_run"):
            print("以上为 dry-run，未真正提交。加 --apply 才会写回 PR。")

    # --- 退出码 ---
    if args.ci and result.should_block():
        return 1
    if result.errors and not result.findings:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pr-review-agent",
        description="PR 代码审查 Agent：规则库 + 大模型，按安全/性能/规范三维自动审查并回评 PR。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py --doctor
  python main.py --rules
  python main.py --diff examples/demo.patch --mock --out runs
  python main.py --diff examples/demo.patch --mock --json
  python main.py --local-diff HEAD~1..HEAD --rules-only
  python main.py --pr owner/repo#42 --mock --post --out runs
  python main.py --pr owner/repo#42 --post --apply --ci
""",
    )
    p.add_argument("--doctor", action="store_true", help="环境自检")
    p.add_argument("--rules", action="store_true", help="打印缺陷模式库")

    p.add_argument("--diff", help="本地 unified diff 文件，- 表示从标准输入读")
    p.add_argument("--local-diff", help="用 git diff 生成，格式 base..head，如 HEAD~1..HEAD")
    p.add_argument("--pr", help="GitHub PR，格式 owner/repo#123 或 PR 页面链接")

    p.add_argument("--mock", action="store_true", help="用离线模型替身，不调真实 API")
    p.add_argument("--rules-only", action="store_true", help="只跑规则库，零 token 成本")

    p.add_argument("--post", action="store_true", help="把审查结果写回 PR")
    p.add_argument("--apply", action="store_true", help="与 --post 配合：真正提交（默认 dry-run）")
    p.add_argument("--block", action="store_true", help="有高危问题时用 REQUEST_CHANGES 而非 COMMENT")

    p.add_argument("--out", help="报告输出目录")
    p.add_argument("--stem", default="review", help="报告文件名前缀，默认 review")
    p.add_argument("--json", action="store_true", help="同时把 JSON 打到标准输出")
    p.add_argument("--ci", action="store_true", help="CI 模式：存在高危问题时退出码为 1")

    return p


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.doctor:
        return cmd_doctor(args)
    if args.rules:
        return cmd_rules(args)
    if not (args.diff or args.local_diff or args.pr):
        parser.print_help()
        return 0
    return cmd_review(args)


if __name__ == "__main__":
    raise SystemExit(main())
