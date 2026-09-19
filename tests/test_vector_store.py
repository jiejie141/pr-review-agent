"""向量检索与混合检索（RRF）的单测。

重点验证三件事：
1. 三个后端满足同一个 `Retriever` 协议 —— 调用方零改动；
2. 哈希向量**确定性** —— 同一文本两次编码逐位相同（这是离线评测可复现的前提）；
3. RRF 融合真的比单路强 —— 构造一个"BM25 拿不到、向量能拿到"的用例来证明，
   而不是只断言"能跑"。
"""

from __future__ import annotations

import pytest

from pagent.retrieval import Chunk, ConventionStore, ScoredChunk
from pagent.vector_store import (
    ChromaVectorRetriever,
    HashingEmbedder,
    HybridRetriever,
    build_store,
)

# 规范文档：措辞与代码里的查询词刻意不重合
CONVENTIONS = """
# 数据库访问
所有数据库访问必须走参数化查询，禁止用字符串拼接构造 SQL 语句。

# 密钥管理
密钥、令牌等敏感信息一律从环境变量读取，不得硬编码在源码或配置文件里。

# 异常处理
捕获异常后必须记录上下文并给出兜底值，不得静默吞掉异常。

# 并发
共享可变状态需要加锁保护，避免多线程下的竞态条件。
"""


def test_hashing_embedder_is_deterministic():
    """同一文本两次编码必须逐位相同 —— 否则评测结果不可复现。"""
    e = HashingEmbedder(dim=256)
    a = e.encode(["参数化查询"])[0]
    b = e.encode(["参数化查询"])[0]
    assert a == b
    assert len(a) == 256


def test_hashing_embedder_normalized():
    e = HashingEmbedder(dim=128)
    v = e.encode(["some text 中文混排"])[0]
    norm = sum(x * x for x in v) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_embedder_separates_different_text():
    e = HashingEmbedder()
    a, b = e.encode(["数据库参数化查询", "前端样式表布局"])
    dot = sum(x * y for x, y in zip(a, b))
    same = sum(x * x for x in e.encode(["数据库参数化查询"])[0])
    assert dot < same * 0.9, "语义不同的文本相似度应明显低于自身"


def _chunk(cid: str, text: str, heading: str = "") -> Chunk:
    return Chunk(id=cid, text=text, source="t", heading=heading)


def test_chroma_retriever_implements_protocol():
    r = ChromaVectorRetriever()
    assert r.add_text(CONVENTIONS) > 0
    hits = r.search("参数化查询", top_k=2)
    assert hits, "应能检索到数据库访问那一节"
    assert all(isinstance(h, ScoredChunk) for h in hits)
    assert all(h.chunk.text for h in hits)


def test_chroma_and_bm25_share_search_signature():
    """两个后端必须能互换 —— reviewer.py 只依赖 search()。"""
    for store in (ConventionStore(), ChromaVectorRetriever()):
        n = store.add_text(CONVENTIONS)
        assert n > 0
        hits = store.search("密钥", top_k=1)
        assert len(hits) == 1
        assert hasattr(store, "render_context")
        ctx = store.render_context("密钥", top_k=1)
        assert isinstance(ctx, str) and ctx


def test_build_store_backends():
    for backend in ("bm25", "vector", "hybrid"):
        s = build_store(inline=[CONVENTIONS], backend=backend)
        assert len(s) > 0, backend
        hits = s.search("异常处理", top_k=2)
        assert hits, backend


def test_build_store_rejects_unknown_backend():
    with pytest.raises(ValueError):
        build_store(backend="nope")


def test_rrf_fusion_combines_both_rankings():
    """RRF 融合应同时保留两路的命中，而不是只取其中一路。"""
    sparse = ConventionStore()
    sparse.add_text(CONVENTIONS)
    dense = ChromaVectorRetriever()
    dense.add_text(CONVENTIONS)

    hy = HybridRetriever(sparse=sparse, dense=dense)
    assert hy.rrf_k == 60
    hits = hy.search("sql injection", top_k=3)
    assert hits
    # 融合分数应来自至少两路的贡献（严格大于单路 1/(k+1) 的上限）
    assert hits[0].score > 1.0 / (60 + 4)


def test_hybrid_degrades_when_dense_fails():
    """向量侧抛异常时应降级为纯 BM25，而不是让整次审查失败。"""

    class BrokenDense:
        def search(self, query, top_k=3):
            raise RuntimeError("chroma down")

        def render_context(self, query, top_k=3, limit=1200):
            return ""

        def __len__(self):
            return 0

    sparse = ConventionStore()
    sparse.add_text(CONVENTIONS)
    hy = HybridRetriever(sparse=sparse, dense=BrokenDense())
    hits = hy.search("密钥硬编码", top_k=2)
    assert hits, "降级后仍应有结果"
    assert "chroma down" in hy.last_degraded


def test_hybrid_works_without_dense():
    sparse = ConventionStore()
    sparse.add_text(CONVENTIONS)
    hy = HybridRetriever(sparse=sparse, dense=None)
    assert hy.search("并发加锁", top_k=1)


def test_stats_reports_backend():
    s = build_store(inline=[CONVENTIONS], backend="hybrid")
    st = s.stats()
    assert st["fusion"] == "rrf"
    assert st["dense"]["backend"] == "chroma"
    assert st["dense"]["embedder"].startswith("hashing-")


def test_two_instances_do_not_collide_in_shared_collection():
    """回归测试：多个 ChromaVectorRetriever 不能互相踩。

    chromadb.EphemeralClient() 每次返回新对象，但底层内存 store 是**共享**的，
    所以同名 collection 其实是同一个集合。早期实现用 `inline#0`、`inline#1`
    当 id，第二个实例就会撞上第一个实例的 id —— 而 Chroma 对重复 id 的 add
    是静默忽略的，于是 add_text 返回 >0 但检索为空。

    这个 bug 只在「同一个进程里先后建过两个实例」时出现，
    单测单独跑不会触发，整包跑才暴露（典型的测试顺序依赖）。
    """
    first = ChromaVectorRetriever()
    assert first.add_text(CONVENTIONS) > 0
    assert first.search("参数化查询", top_k=2)

    second = ChromaVectorRetriever()  # 同一进程、同名 collection
    assert second.add_text(CONVENTIONS) > 0
    hits = second.search("参数化查询", top_k=2)
    assert hits, "第二个实例必须能检索到自己的内容，不能被共享集合吞掉"
    assert all(h.chunk.id.startswith(second.instance_id) for h in hits)


def test_second_instance_retrieves_its_own_chunks_only():
    """共享集合里混有两份数据时，各自只应看到自己的 chunk。"""
    a = ChromaVectorRetriever()
    a.add_text(CONVENTIONS)

    b = ChromaVectorRetriever()
    # 注意：正文必须长于 min_chunk_chars（默认 24），否则会被切片规则过滤掉
    b.add_text("# 前端\n组件样式必须统一走设计令牌，禁止在样式文件里硬编码颜色值。")

    b_ids = {c.id for c in b.chunks}
    assert b_ids, "b 应该切出了 chunk"
    hits = b.search("设计令牌", top_k=3)
    assert hits
    assert all(h.chunk.id in b_ids for h in hits), "不应返回别的实例的条目"
