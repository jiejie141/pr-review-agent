"""审查编排：规则层 + 模型层 → 合并、校验、去重 → 最终意见集。

三道闸门，缺一不可：

1. **分片闸门** —— 按文件切、按字数切。一个大 PR 直接塞进去会超上下文，
   而且模型看到几千行时会「只读开头」。
2. **锚定闸门** —— 模型报的行号必须在 diff 的新增行集合里。不在的一律丢掉并计数。
   这是压幻觉最有效的一步：编造的行号会在这里被拦下，而不是发到 PR 上丢人。
3. **去重闸门** —— 规则和模型经常同时命中同一个问题；按 (文件, 行, 维度) 合并，
   保留证据更硬的那条，并在另一条上标记「双方一致」以提高置信度。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, get_settings
from .diffparse import index_added_lines, parse_unified_diff
from .llm import LLMError, LLMClient, MockLLMClient, build_real_client  # noqa: F401
from .models import (
    LineKind,
    Category,
    DiffFile,
    Finding,
    ReviewResult,
    ReviewStats,
    Severity,
    SourceKind,
)
from .retrieval import ConventionStore
from .rules import RuleEngine
from .vector_store import build_store

SYSTEM_PROMPT = """你是一名资深代码评审者，正在审查一个 Pull Request 的 diff。

你只审查**新增或修改的行**（下面用 `行号:` 标出的行），不要评论未改动的历史代码。

请从三个维度审查，每条意见只能属于其中一个维度：

- **security**：注入（SQL/命令/模板）、越权、凭据硬编码、不安全反序列化、
  关闭 TLS 校验、弱哈希存口令、SSRF、路径穿越、敏感信息进日志、调试开关上线。
- **performance**：循环内查库（N+1）、循环内建连接或编译正则、异步中同步阻塞、
  未分页全量加载、嵌套循环做线性查找、重复计算未缓存。
- **convention**：吞异常、裸 except、可变对象作默认参数、文件句柄未用 with、
  用 assert 做输入校验、调试打印残留、未跟踪的 TODO、命名与项目约定不符。

## 硬性规则（违反的意见会被程序丢弃）

1. **必须给出行号**。每条意见都要带 `line`，且该行号必须是上面列出的、带 `行号:` 前缀的行。
   不要报历史行，不要估计行号，不要报文件级问题（那类问题直接不要提）。
2. **必须引用原文**。`evidence` 填该行的关键代码片段，用于人工复核。
3. **无证据不评论**。只是「感觉不太好」「建议优化」而说不出具体风险的意见，不要提。
4. **命名与风格类问题只在明显违反仓库约定时才提**。仓库约定会以「仓库规范」形式提供，
   没有冲突就不要提。
5. **不确定的问题只标注风险**，把 severity 设为 low，并在 suggestion 里说明为什么拿不准。
6. 每条意见必须给出**可执行的修改建议**，不要只说「有问题」。

## 输出格式

只输出一个 JSON 对象，不要有任何解释性文字、不要 markdown 围栏：

{"findings": [
  {
    "line": 42,
    "title": "简短标题（20 字以内）",
    "category": "security|performance|convention",
    "severity": "high|medium|low",
    "evidence": "该行关键代码片段",
    "suggestion": "具体怎么改，一到两句",
    "confidence": 0.0 到 1.0 之间的小数
  }
]}

没有发现任何问题时输出 {"findings": []}。宁缺毋滥，一条站不住的意见比十条有价值的意见代价更大。
"""


# 这些文件通常不是人写的，审查它们纯属浪费 token
SKIP_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".jar",
    ".pyc", ".class", ".so", ".dll", ".exe", ".snap",
)
SKIP_PATH_PARTS = (
    "node_modules/", "vendor/", "dist/", "build/", "target/", ".venv/",
    "venv/", "__pycache__/", "package-lock.json", "poetry.lock", "yarn.lock",
    "pnpm-lock.yaml", "go.sum", "third_party/", "generated/",
)
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _skip_file(f: DiffFile) -> str | None:
    """返回跳过原因，None 表示需要审查。"""
    if f.is_binary:
        return "binary"
    if f.is_deleted:
        return "deleted"
    if f.added_line_count == 0:
        return "no-added-lines"
    p = f.path.lower()
    if any(p.endswith(s) for s in SKIP_SUFFIXES):
        return "generated-or-binary-suffix"
    if any(part in p for part in SKIP_PATH_PARTS):
        return "vendored"
    return None


@dataclass
class Shard:
    """一个送给模型的最小审查单元。"""

    file: str
    text: str
    line_numbers: list[int]
    truncated: bool = False

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass
class ReviewerConfig:
    max_findings_per_file: int = 6
    min_severity: str = "low"
    require_anchor: bool = True
    max_files: int = 60
    shard_max_chars: int = 6000
    shard_max_files: int = 8


def build_shards(files: list[DiffFile], max_chars: int = 6000, max_files: int = 8) -> list[Shard]:
    """按字符预算分片，粒度到「行」。

    为什么是行级而不是 hunk 级：一个新增文件在 diff 里就是**一个巨大的 hunk**，
    按 hunk 切的话整个文件会挤进同一个分片，直接撑爆上下文窗口。
    按行切能让任意大小的文件都切成等长的块。

    代价是分片边界可能落在函数中间。权衡下来这是更小的代价：
    模型看不到完整函数时最多少提一条意见，而撑爆上下文会让整个分片直接失败。
    """
    shards: list[Shard] = []
    for f in files:
        header = f"### 文件: {f.path}"
        units: list[tuple[str, int | None]] = []  # (渲染文本, 新增行号)
        for h in f.hunks:
            units.append(
                (f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@ {h.header}".rstrip(), None)
            )
            for ln in h.lines:
                if ln.kind is LineKind.NO_NEWLINE:
                    continue
                mark = f"{ln.new_no}: " if ln.new_no is not None else "  - "
                units.append((f"{mark}{ln.kind.value}{ln.text}", ln.new_no if ln.is_added else None))

        units = [(t, n) for t, n in units if t.strip()]
        if not units:
            continue

        total = len(header) + 1 + sum(len(t) + 1 for t, _ in units)
        if total <= max_chars:
            shards.append(
                Shard(
                    file=f.path,
                    text="\n".join([header] + [t for t, _ in units]),
                    line_numbers=[n for _, n in units if n is not None],
                )
            )
            continue

        cur_lines = [header]
        cur_nums: list[int] = []
        used = len(header) + 1
        for text, num in units:
            if used + len(text) + 1 > max_chars and cur_nums:
                shards.append(Shard(file=f.path, text="\n".join(cur_lines), line_numbers=cur_nums))
                cont = f"### 文件: {f.path} (续)"
                cur_lines = [cont]
                cur_nums = []
                used = len(cont) + 1
            cur_lines.append(text)
            used += len(text) + 1
            if num is not None:
                cur_nums.append(num)
        if cur_nums:
            shards.append(Shard(file=f.path, text="\n".join(cur_lines), line_numbers=cur_nums))

    return shards[: max(1, max_files * 8)]


class Reviewer:
    """把 diff 变成一组经过校验的审查意见。"""

    def __init__(
        self,
        settings: Settings | None = None,
        llm: Any | None = None,
        conventions: ConventionStore | None = None,
        config: ReviewerConfig | None = None,
        enabled: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm
        self.enabled = enabled
        self.config = config or ReviewerConfig(
            max_findings_per_file=self.settings.max_findings_per_file,
            min_severity=self.settings.min_severity,
            require_anchor=self.settings.require_line_anchor,
            max_files=self.settings.max_files,
            shard_max_chars=self.settings.shard_max_chars,
            shard_max_files=self.settings.shard_max_files,
        )
        self.rules = RuleEngine()
        if conventions is not None:
            self.store = conventions
        else:
            # 按 settings.retrieval_backend 选检索器：
            # bm25（默认，零依赖）/ vector（Chroma）/ hybrid（BM25+向量 RRF 融合）。
            # 三种都满足 Retriever 协议，本文件下面的代码完全不需要分支。
            self.store = build_store(
                self.settings.conventions,
                backend=getattr(self.settings, "retrieval_backend", "bm25"),
            )

    # -- 主流程 ------------------------------------------------------------
    def review(self, diff_text: str, pr_title: str = "", pr_url: str = "") -> ReviewResult:
        result = ReviewResult(pr_title=pr_title, pr_url=pr_url)
        stats = result.stats

        files = parse_unified_diff(diff_text)
        if not files:
            result.errors.append("diff 为空或无法解析")
            return result

        # 只保留需要审查的文件
        targets: list[DiffFile] = []
        for f in files:
            if _skip_file(f) is None:
                targets.append(f)
        targets = targets[: self.config.max_files]

        stats.files = len(targets)
        stats.added_lines = sum(f.added_line_count for f in targets)
        if not targets:
            result.errors.append("没有需要审查的新增代码行")
            return result

        added_index = index_added_lines(targets)

        # --- 第一层：规则 ---
        rule_report = self.rules.analyze(targets)
        findings: list[Finding] = list(rule_report.findings)
        stats.rule_findings = len(rule_report.findings)

        # --- 第二层：模型 ---
        if self.enabled and self.llm is not None:
            shards = build_shards(targets, self.config.shard_max_chars, self.config.shard_max_files)
            stats.shards = len(shards)
            for shard in shards:
                try:
                    got, usage = self._review_shard(shard, pr_title)
                except LLMError as e:
                    result.errors.append(f"[{shard.file}] 模型调用失败：{e}")
                    continue
                stats.llm_calls += 1
                stats.prompt_tokens += usage[0]
                stats.completion_tokens += usage[1]
                for fi in got:
                    if fi.line is not None and (fi.file, fi.line) in added_index:
                        fi.anchors_ok = True
                        stats.llm_findings += 1
                        findings.append(fi)
                    else:
                        stats.dropped_unanchored += 1

        # --- 第三层：合并去重 ---
        merged, merged_count = self._merge(findings)
        stats.merged_duplicates = merged_count

        # --- 过滤与截断 ---
        floor = SEV_ORDER.get(self.config.min_severity, 2)
        kept = [f for f in merged if SEV_ORDER.get(f.severity.value, 9) <= floor]
        result.findings = self._cap_per_file(kept)
        result.sort()
        return result

    # -- 单个分片 ----------------------------------------------------------
    def _review_shard(self, shard: Shard, pr_title: str) -> tuple[list[Finding], tuple[int, int]]:
        convention_ctx = self.store.render_context(
            f"{shard.file} {pr_title}".strip() or shard.file, top_k=3
        )
        user_parts: list[str] = []
        if pr_title:
            user_parts.append(f"PR 标题：{pr_title}")
        if convention_ctx:
            user_parts.append(f"## 仓库规范（提意见时以此为准）\n{convention_ctx}")
        else:
            user_parts.append("## 仓库规范\n（未提供，风格类问题请一律不要提）")
        user_parts.append(f"## 待审查的 diff 片段\n```\n{shard.text}\n```")
        user_parts.append(
            "现在按系统提示的要求输出 JSON。记住：line 必须是上面带 `行号:` 前缀的数字之一。"
        )

        resp = self.llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            json_mode=True,
        )
        findings = self._parse_findings(resp.text, shard)
        return findings, (resp.prompt_tokens, resp.completion_tokens)

    def _parse_findings(self, text: str, shard: Shard) -> list[Finding]:
        """解析模型的 JSON。解析失败不抛异常，返回空列表并让上层记一笔。"""
        from .llm import parse_json_payload

        try:
            payload = parse_json_payload(text)
        except LLMError:
            return []

        raw_items: list[dict] = []
        if isinstance(payload, dict):
            got = payload.get("findings") or payload.get("comments") or []
            if isinstance(got, list):
                raw_items = [x for x in got if isinstance(x, dict)]
        elif isinstance(payload, list):
            raw_items = [x for x in payload if isinstance(x, dict)]

        out: list[Finding] = []
        for item in raw_items:
            fi = self._to_finding(item, shard)
            if fi is not None:
                out.append(fi)
        return out

    def _to_finding(self, item: dict, shard: Shard) -> Finding | None:
        line = item.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line.strip())
        elif isinstance(line, float):
            line = int(line)
        elif not isinstance(line, int):
            line = None

        # 行号存在但不在本分片内 → 视为幻觉，置 None 交给外层丢弃
        anchors_ok = line is not None and line in shard.line_numbers

        title = str(item.get("title") or "").strip() or "模型指出的问题"
        cat_raw = str(item.get("category") or "convention").strip().lower()
        sev_raw = str(item.get("severity") or "low").strip().lower()
        try:
            category = Category(cat_raw)
        except ValueError:
            category = Category.CONVENTION
        try:
            severity = Severity(sev_raw)
        except ValueError:
            severity = Severity.LOW

        conf = item.get("confidence", 0.5)
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf = 0.5

        return Finding(
            # 模型不给规则号，用 LLM 前缀 + 维度，保证报告里可区分来源
            rule_id=f"LLM-{category.value[:3].upper()}",
            title=title[:80],
            category=category,
            severity=severity,
            # 关键：文件以分片为准，不信模型给的路径
            file=shard.file,
            line=line,
            evidence=str(item.get("evidence") or "").strip()[:240],
            suggestion=str(item.get("suggestion") or "").strip()[:500],
            confidence=conf,
            source=SourceKind.LLM,
            anchors_ok=anchors_ok,
        )

    # -- 合并 --------------------------------------------------------------
    @staticmethod
    def _merge(findings: list[Finding]) -> tuple[list[Finding], int]:
        """按 (文件, 行, 维度) 合并。

        规则与模型同时命中同一处时，保留规则那条（证据更硬、可复现），
        把置信度抬高，并在 extra 里标记 `corroborated`，报告里会显示「规则+模型一致」。
        """
        buckets: dict[str, list[Finding]] = {}
        for f in findings:
            buckets.setdefault(f.fingerprint, []).append(f)

        out: list[Finding] = []
        merged = 0
        for group in buckets.values():
            if len(group) == 1:
                out.append(group[0])
                continue
            merged += len(group) - 1
            group.sort(key=lambda f: (f.source is SourceKind.LLM, -f.confidence))
            keep = group[0]
            if any(g.source is SourceKind.LLM for g in group) and any(
                g.source is SourceKind.RULE for g in group
            ):
                keep.extra["corroborated"] = True
                keep.confidence = min(1.0, keep.confidence + 0.1)
                titles = {g.title for g in group if g.title}
                if len(titles) > 1:
                    keep.extra["alt_titles"] = sorted(titles)
            out.append(keep)
        return out, merged

    def _cap_per_file(self, findings: list[Finding]) -> list[Finding]:
        """单文件意见数封顶。

        PR 上挂 40 条意见没人会看完，信息密度反而下降。
        按严重级别排序后取前 N 条，其余丢弃（并在统计里体现）。
        """
        cap = self.config.max_findings_per_file
        per_file: dict[str, list[Finding]] = {}
        for f in findings:
            per_file.setdefault(f.file, []).append(f)
        out: list[Finding] = []
        for group in per_file.values():
            group.sort(key=lambda f: (f.severity.rank, -f.confidence))
            out.extend(group[:cap])
        return out


def review_diff_text(
    diff_text: str,
    settings: Settings | None = None,
    mock: bool | None = None,
    pr_title: str = "",
    pr_url: str = "",
    use_llm: bool = True,
) -> ReviewResult:
    """便捷入口：一次性完成审查，适合脚本和测试调用。"""
    st = settings or get_settings()
    llm = None
    if use_llm:
        if mock is True or (mock is None and st.mock):
            llm = MockLLMClient()
        elif st.llm_ready:
            llm = build_real_client(st)
    rv = Reviewer(settings=st, llm=llm, enabled=use_llm)
    return rv.review(diff_text, pr_title=pr_title, pr_url=pr_url)
