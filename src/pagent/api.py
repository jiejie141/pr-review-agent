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

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

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
    mode: Literal["rules", "mock", "live"] = Field(
        "rules",
        description=(
            "审查模式：`rules` 只跑确定性规则层（零网络、零 token）；"
            "`mock` 额外跑离线替身的语义层（零网络、零 token，指标里的 token "
            "是估算值仅供链路验证）；`live` 调用真实 LLM（会产生费用与网络流量）。"
        ),
    )
    mock: bool | None = Field(
        None,
        description=(
            "【已废弃，请改用 mode】语义模糊的二值开关：它只决定 LLM 提供方是真客户端"
            "还是离线替身，并不决定是否启用语义层。保留仅为兼容旧调用方。"
        ),
    )
    use_llm: bool | None = Field(
        None,
        description="【已废弃，请改用 mode】是否启用 LLM 语义层。",
    )
    retrieval_backend: str | None = Field(
        None, description="覆盖检索后端：bm25 / vector / hybrid"
    )

    # 不在 Field 上写 `deprecated=True`：pydantic 的该参数会让**每次实例化都发
    # DeprecationWarning**（连只用 mode 的调用方也逃不掉），警告噪声会盖住真正
    # 的信号。这里改成在 OpenAPI 里把它标成废弃，文档照旧提示，运行时保持安静。
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"diff": "<unified diff>", "mode": "rules"},
                {"diff": "<unified diff>", "mode": "mock", "pr_title": "示例 PR"},
            ]
        }
    )

    def resolve_mode(self) -> str:
        """把新旧参数归一成 `rules` / `mock` / `live` 三态。

        这里花了点篇幅，因为这个字段是**最容易出安全隐患**的地方：
        它同时决定"花不花钱"和"发不发包"。旧的两个布尔是**相乘**语义
        （`use_llm` 决定开不开启语义层，`mock` 只决定用真客户端还是替身），
        而默认值 `mock=True, use_llm=True` 组合出来的却是**真实付费调用** ——
        "离线替身"的字面意思和实际行为正好相反。

        兼容映射按调用方的**意图**来读，而不是按字段名：
        - 只给 `mock`：`mock=True` 的人想要的是"别真的调模型"，所以给 `mock` 模式；
        - 只给 `use_llm`：`use_llm=False` 想要"什么都别调" -> `rules`；
          留空或 `True` 则在有 Key 时走 `live`（保持旧行为，但不再由 `mock` 默认值兜住）；
        - 都没给：`rules` —— 默认永远不产生外部请求。
        """
        if self.mock is not None and self.use_llm is None:
            return "mock" if self.mock else "live"
        if self.use_llm is not None and self.mock is None:
            return "live" if self.use_llm else "rules"
        if self.mock is not None and self.use_llm is not None:
            # 两个都给：沿用旧版的相乘语义，把可能的组合映射到最接近的一态。
            if not self.use_llm:
                return "rules"
            return "mock" if self.mock else "live"
        return self.mode


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


# ---------------------------------------------------------------------------
# 控制台页面
#
# 用 FileResponse 直接吐静态 HTML：不引模板引擎、不挂 StaticFiles。
# 页面只有一个文件、没有构建链，再套一层模板或静态目录只是多余的间接层。
# 放在包内（src/pagent/web/）以便 `pip install` 后仍能找到 —— 路径基于
# __file__ 推导，不依赖当前工作目录。
# ---------------------------------------------------------------------------
_WEB_INDEX = Path(__file__).resolve().parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    """返回审查控制台页面。"""
    if not _WEB_INDEX.is_file():
        raise HTTPException(404, "控制台页面缺失：src/pagent/web/index.html")
    return FileResponse(_WEB_INDEX, media_type="text/html; charset=utf-8")


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
    # 用 get_settings() 而不是 get_settings(reload=True)：
    # 每次请求都 reload 会重新读一遍 .env 并**替换掉全局单例对象**，
    # 于是"检查时看到的配置"和"真正用来调模型的那份配置"可能不是同一个实例。
    # 这种不一致本身就是隐患 —— 检查说没 Key、调用时说有 Key（或反过来），
    # 排查时几乎不可能靠读代码看出来。配置只在启动/显式 reload 时取一次。
    st = get_settings()
    if req.retrieval_backend:
        if req.retrieval_backend not in ("bm25", "vector", "hybrid"):
            raise HTTPException(
                400, f"未知检索后端 {req.retrieval_backend}（可选 bm25 / vector / hybrid）"
            )
        # ⚠️ 不能写成 st.retrieval_backend = ...：st 是 get_settings() 的
        # 全局单例，直接改会把这次请求的覆盖**泄漏给后续所有请求**
        # （/health 的显示也会被带走），并发请求还会互相踩。
        # 用 replace 复制一份再改，单例保持不动。
        st = replace(st, retrieval_backend=req.retrieval_backend)

    mode = req.resolve_mode()
    if mode == "live" and not st.llm_ready:
        # 明确告诉调用方"你要求的真实调用做不了"，而不是悄悄退回替身
        # ——静默降级的报告会被当成真实模型结论去用，那才是真危险。
        raise HTTPException(
            400,
            "mode=live 需要配置 LLM_API_KEY；当前未配置。"
            "若要离线体验语义层请用 mode=mock，只跑规则层请用 mode=rules。",
        )

    try:
        result = review_diff_text(
            req.diff,
            settings=st,
            mock=(mode != "live"),
            pr_title=req.pr_title,
            pr_url=req.pr_url,
            use_llm=(mode != "rules"),
        )
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    out = _to_out(result)
    out.stats = dict(out.stats, mode=mode)
    return out


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)
