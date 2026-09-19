"""FastAPI 服务层。

## 为什么 PR 审查也需要一个 HTTP 层

这个项目的原生用法是 CLI + GitHub Action，看起来"不需要服务"。
但有两个真实场景绕不开 HTTP：

1. **GitHub Action 之外的接入点**：GitLab CI、Jenkins、内部 CI 平台
   要触发审查，最通用的方式就是调一个 HTTP 接口；
2. **本地预检**：开发者想在 push 前先看看会报什么，一个
   `POST /review` 比装 CLI 再学参数简单得多。

所以这不是"为了写 FastAPI 而写"，而是把已有的 `review_diff_text()`
暴露成一个更通用的入口。**服务层不含任何审查逻辑**——
它只做「收 diff → 调 reviewer → 序列化」，避免 CLI 与 API 行为漂移。

启动：
    uvicorn pagent.api:app --reload --port 8100
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings
from .models import ReviewResult
from .reviewer import review_diff_text

app = FastAPI(
    title="pr-review-agent API",
    version="1.0.0",
    description=(
        "两层架构的 PR 代码审查 Agent：28 条确定性规则 + LLM 语义层，"
        "行号锚定强制校验，规则与模型结果按 (文件, 行, 维度) 合并。\n\n"
        "支持三种检索后端：`bm25`（默认，零依赖）/ `vector`（Chroma）/ "
        "`hybrid`（BM25+向量 RRF 融合）。"
    ),
)


class ReviewRequest(BaseModel):
    diff: str = Field(..., min_length=1, description="unified diff 文本")
    pr_title: str = Field("", max_length=300)
    pr_url: str = Field("", max_length=500)
    mock: bool = Field(True, description="离线替身模式（默认开，不消耗 token）")
    use_llm: bool = Field(True, description="是否启用 LLM 语义层")
    retrieval_backend: str | None = Field(
        None, description="覆盖检索后端：bm25 / vector / hybrid"
    )


class FindingOut(BaseModel):
    rule_id: str
    title: str
    category: str
    severity: str
    file: str
    line: int | None = None
    evidence: str = ""
    suggestion: str = ""
    confidence: float = 0.0
    source: str
    anchors_ok: bool = False
    corroborated: bool = False


class ReviewOut(BaseModel):
    pr_title: str = ""
    pr_url: str = ""
    findings: list[FindingOut] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


def _stats_to_dict(stats: Any) -> dict[str, Any]:
    """ReviewStats 是 @dataclass，不是 pydantic 模型。

    这里必须显式走 `to_dict()` —— 之前写成
    `stats.model_dump() if hasattr(...) else {}`，因为 dataclass 没有
    `model_dump`，所以**静默返回了空字典**，接口看着 200 但指标全丢。
    这类"字段悄悄变空"的 bug 不会报错，只能靠断言 stats 内容才发现。
    """
    to_dict = getattr(stats, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(stats, dict):
        return stats
    return {}


def _to_out(r: ReviewResult) -> ReviewOut:
    return ReviewOut(
        pr_title=r.pr_title,
        pr_url=r.pr_url,
        findings=[
            FindingOut(
                rule_id=f.rule_id,
                title=f.title,
                category=f.category.value,
                severity=f.severity.value,
                file=f.file,
                line=f.line,
                evidence=f.evidence,
                suggestion=f.suggestion,
                confidence=f.confidence,
                source=f.source.value,
                anchors_ok=f.anchors_ok,
                corroborated=bool(f.extra.get("corroborated")),
            )
            for f in r.findings
        ],
        stats=_stats_to_dict(r.stats),
        errors=list(r.errors),
    )


@app.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    st = get_settings()
    vector_ok = True
    try:
        import chromadb  # noqa: F401
    except ImportError:
        vector_ok = False
    return {
        "status": "ok",
        "retrieval_backend": st.retrieval_backend,
        "chroma_available": vector_ok,
        "llm_configured": bool(st.llm_api_key),
        "require_line_anchor": st.require_line_anchor,
    }


@app.get("/backends", summary="可用检索后端")
def backends() -> dict[str, Any]:
    st = get_settings()
    try:
        import chromadb  # noqa: F401

        chroma = True
    except ImportError:
        chroma = False
    return {
        "default": st.retrieval_backend,
        "available": ["bm25"] + (["vector", "hybrid"] if chroma else []),
        "notes": {
            "bm25": "纯标准库实现，零依赖、可复现",
            "vector": "Chroma 向量检索（需 pip install chromadb）",
            "hybrid": "BM25 + 向量，RRF 融合；向量侧不可用时自动降级为纯 BM25",
        },
    }


@app.post("/review", response_model=ReviewOut, summary="审查一段 diff")
def review(req: ReviewRequest) -> ReviewOut:
    st = get_settings(reload=True)
    if req.retrieval_backend:
        if req.retrieval_backend not in ("bm25", "vector", "hybrid"):
            raise HTTPException(
                400, f"未知检索后端 {req.retrieval_backend}（可选 bm25 / vector / hybrid）"
            )
        st.retrieval_backend = req.retrieval_backend

    try:
        result = review_diff_text(
            req.diff,
            settings=st,
            mock=req.mock,
            pr_title=req.pr_title,
            pr_url=req.pr_url,
            use_llm=req.use_llm,
        )
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    return _to_out(result)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)
