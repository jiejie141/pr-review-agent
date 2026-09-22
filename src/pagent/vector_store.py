"""向量检索与混合检索（BM25 + 向量，RRF 融合）。

## 和 retrieval.py 的分工

`retrieval.py` 里的 `ConventionStore` 是**纯标准库的 BM25 实现**，
它的价值是「零依赖 + 可复现 + 我读过 BM25 的公式」。
但 BM25 有一个结构性短板：**只看词面，不看语义**。

规范文档里的查询恰恰经常是语义查询。例如代码里出现：

    cursor.execute(f"SELECT * FROM users WHERE id = {uid}")

规则层能抓到 `f-string 拼 SQL`（词面命中）。但如果规范文档里写的是
「所有数据库访问必须走参数化查询」，而查询词是 `sql injection`——
BM25 因为「注入」和「参数化」没有共享词元而完全检索不到，
向量检索则能靠语义把它们拉到一起。

所以这一层做的是**互补**，不是替换：
两条检索路径各自召回，再用 **RRF（Reciprocal Rank Fusion）** 融合排名。

## 为什么用 RRF 而不是加权求和

BM25 的分数是无界的（可能 0.3，也可能 30），余弦相似度是 [-1,1]。
直接加权求和需要先归一化，而归一化会把两路的分布强行拉到同一尺度，
反而丢掉了「BM25 认为这条异常匹配」这种信息。
RRF 只用**排名**，天然规避了量纲问题，而且被证明在多路召回融合上非常稳：

    score(d) = Σ_r  1 / (k + rank_r(d))          # k 常取 60

## Embedding 的选择

默认用 `HashingEmbedder`——**确定性哈希向量 + 字符/词混合 shingle**。
理由：
1. 评测要可复现。真实 embedding API 每次调用可能有微小数值差异，
   而「留出集召回 2/2」这类结论必须是跑一百次都一样；
2. CI 里不能依赖外部 API（会要 key、会限流、会超时）；
3. 语义能力确实弱于真模型，但**对于「中英混排的技术规范文档」，
   字符级 shingle 已经能提供 BM25 拿不到的模糊匹配能力**。

需要真实语义时实现 `Embedder` 协议接入 bge / text-embedding-3 即可，
`ChromaVectorRetriever` 不需要改。
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import uuid
from dataclasses import dataclass
from typing import Iterable, Protocol

from .retrieval import Chunk, ConventionStore, Retriever, ScoredChunk, tokenize

# RRF 的平滑常数。60 是原论文推荐的取值，实践中对结果不敏感。
RRF_K = 60


class Embedder(Protocol):
    """把文本映射成固定维度向量。"""

    dim: int
    name: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """确定性哈希向量（feature hashing）。

    不是"假 embedding"——它是一个真的向量空间模型：
    - 特征：英文词 + 中文二元组 + 字符级 3-gram shingle
    - 用 blake2b 做 feature hashing 映射到固定维度（避免维护词表）
    - 带符号哈希（sign hashing）+ L2 归一化，减少哈希碰撞带来的偏差

    字符级 shingle 是它比 BM25 强的地方：`参数化查询` 和 `parameterized query`
    没有共同词元，但共享大量字符片段时仍能拿到非零相似度。
    """

    def __init__(self, dim: int = 512, use_char_shingles: bool = True) -> None:
        self.dim = dim
        self.use_char_shingles = use_char_shingles
        self.name = f"hashing-{dim}{'-char' if use_char_shingles else ''}"

    def _features(self, text: str) -> list[str]:
        feats = tokenize(text)
        if self.use_char_shingles:
            # 紧邻的字符 3-gram。混排文本里中文没有空格，
            # 3-gram 能把「参数化」拆成 参数化/数化查/化查询…，对模糊匹配很有效。
            compact = re.sub(r"\s+", " ", text.lower())
            for seg in re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", compact):
                for i in range(len(seg) - 2):
                    feats.append("#" + seg[i : i + 3])
        return feats

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        v = int.from_bytes(h, "big")
        return v % self.dim, 1.0 if (v >> 63) & 1 else -1.0

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dim
            for f in self._features(t):
                idx, sign = self._bucket(f)
                vec[idx] += sign
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


@dataclass
class _Hit:
    chunk: Chunk
    score: float


class ChromaVectorRetriever:
    """基于 Chroma 的向量检索器，实现与 `ConventionStore` 相同的 `Retriever` 协议。

    「接口留了 Retriever 协议」这句话在 retrieval.py 的注释里就写了，
    这个类就是兑现——**调用方一行都不用改**（reviewer.py 只依赖 `search`）。
    """

    # 同一个 collection 名在同一个 client 里是**全局共享**的：
    # chromadb.EphemeralClient() 每次返回新对象，但底层指向同一份内存 store，
    # 所以 `get_or_create_collection("conventions")` 拿到的其实是同一个集合。
    # 若用「序号」当 id（inline#0、inline#1…），第二个实例就会和第一个实例
    # 撞 id，而 Chroma 对重复 id 的 add 是**静默忽略**的 —— 表现为 add_text
    # 返回 >0 但 collection.count() 不涨，随后 search() 返回空。
    # 因此 id 必须加上实例级前缀，保证多次实例化互不干扰。
    _INSTANCE_SEQ = itertools.count()

    def __init__(
        self,
        embedder: Embedder | None = None,
        collection_name: str = "conventions",
        persist_dir: str | None = None,
        min_chunk_chars: int = 24,
        instance_id: str | None = None,
    ) -> None:
        import chromadb  # 延迟导入：不用向量检索时不需要装 chromadb

        self.embedder = embedder or HashingEmbedder()
        self.min_chunk_chars = min_chunk_chars
        self.chunks: list[Chunk] = []
        # 实例前缀：默认用进程内自增序号 + 随机串，测试/多实例场景下天然隔离
        self.instance_id = instance_id or (
            f"{next(ChromaVectorRetriever._INSTANCE_SEQ)}-{uuid.uuid4().hex[:6]}"
        )
        if persist_dir:
            self._client = chromadb.PersistentClient(path=persist_dir)
        else:
            self._client = chromadb.EphemeralClient()
        self._col = self._client.get_or_create_collection(
            name=collection_name,
            # 我们自己给向量，不让 Chroma 调它的默认 embedding 函数
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        self._next = 0
        # 已同步到 Chroma 的**本实例** chunk 数。
        # 不能用 self._col.count()：collection 是共享的，它的总数包含别的实例
        # 写进去的条目，一旦大于本实例 chunks 长度，切片就会永远为空，
        # 于是 add_text 之后再也不会触发 _sync()。
        self._synced = 0

    # -- 索引（与 ConventionStore.add_text 同样的切块规则）-------------------
    def add_text(self, text: str, source: str = "inline") -> int:
        added = 0
        heading = ""
        buf: list[str] = []

        def flush() -> None:
            nonlocal buf, added
            body = "\n".join(buf).strip()
            buf = []
            if len(body) < self.min_chunk_chars:
                return
            cid = f"{self.instance_id}#{self._next}"
            self._next += 1
            indexable = f"{heading}\n{body}" if heading else body
            self.chunks.append(
                Chunk(
                    id=cid, text=body, source=source, heading=heading,
                    tokens=tokenize(indexable),
                )
            )
            added += 1

        for raw in text.split("\n"):
            line = raw.rstrip()
            if line.lstrip().startswith("#"):
                flush()
                heading = line.lstrip("#").strip()
                continue
            if not line.strip():
                flush()
                continue
            buf.append(line)
        flush()

        if added:
            self._sync()
        return added

    def _sync(self) -> None:
        """把还没入库的 chunk 写进 Chroma。增量更新，不重复写。

        水位线用**本实例**的 `self._synced`，不用 `self._col.count()` ——
        collection 在 client 内是共享的，count 会包含其他实例写入的条目，
        拿它当切片起点会让 pending 恒为空。
        """
        pending = self.chunks[self._synced:]
        if not pending:
            return
        docs = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in pending]
        self._col.add(
            ids=[c.id for c in pending],
            documents=docs,
            embeddings=self.embedder.encode(docs),
            metadatas=[
                {"source": c.source, "heading": c.heading, "instance": self.instance_id}
                for c in pending
            ],
        )
        self._synced = len(self.chunks)

    def add_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return self.add_text(fh.read(), source=path)

    def add_files(self, paths: Iterable[str]) -> int:
        return sum(self.add_file(p) for p in paths)

    # -- 检索 ---------------------------------------------------------------
    def search(self, query: str, top_k: int = 3) -> list[ScoredChunk]:
        if not self.chunks:
            return []
        n = min(max(top_k, 1), len(self.chunks))
        try:
            res = self._col.query(
                # collection 在 client 内是共享的，会混有别的实例写入的条目。
                # 用 where 按 instance 精确过滤，而不是靠「多取几条再丢弃」——
                # 后者在别人写入量远大于自己时，自己的条目会被挤出 n_results。
                query_embeddings=self.embedder.encode([query]),
                n_results=n,
                where={"instance": self.instance_id},
                include=["distances"],
            )
        except Exception as exc:
            # 不能静默返回 []：纯 vector 模式下这就是"规范检索整个没了"，
            # 而报告里什么都看不出来。记下来并暴露进 stats()，
            # 与 HybridRetriever.last_degraded 保持同一口径（2026-09-22 审查）。
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        self.last_error = ""
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        by_id = {c.id: c for c in self.chunks}
        out: list[ScoredChunk] = []
        for cid, d in zip(ids, dists):
            c = by_id.get(cid)
            if c is None:
                continue
            # cosine 距离 → 相似度，便于和 BM25 分数区分阅读
            out.append(ScoredChunk(chunk=c, score=round(1.0 - float(d), 6)))
        return out

    def render_context(self, query: str, top_k: int = 3, limit: int = 1200) -> str:
        return _render(self.search(query, top_k=top_k), limit)

    def __len__(self) -> int:
        return len(self.chunks)

    def stats(self) -> dict:
        d = {
            "chunks": len(self.chunks),
            "backend": "chroma",
            "embedder": self.embedder.name,
            "dim": self.embedder.dim,
            "sources": sorted({c.source for c in self.chunks}),
        }
        if getattr(self, "last_error", ""):
            d["last_error"] = self.last_error
        return d


class HybridRetriever:
    """BM25 + 向量两路召回，用 RRF 融合。

    这是本文件的主角。设计要点：

    1. **两路各自独立 top_k**，不共用配额。BM25 在「关键词精确命中」上好，
       向量在「换个说法表达同一个意思」上好，配额共享会让强项互相挤掉。
    2. **RRF 只用排名**，避免 BM25 无界分数与余弦相似度的量纲冲突。
    3. **失败降级**：向量侧抛异常（Chroma 没装、维度不匹配）时自动退回纯 BM25，
       而不是让整次审查失败。可用性优先于"必须用上新特性"。
    """

    def __init__(
        self,
        sparse: Retriever,
        dense: Retriever | None,
        rrf_k: int = RRF_K,
        dense_weight: float = 1.0,
    ) -> None:
        self.sparse = sparse
        self.dense = dense
        self.rrf_k = rrf_k
        self.dense_weight = dense_weight
        self.last_degraded = ""

    def search(self, query: str, top_k: int = 3) -> list[ScoredChunk]:
        pool = max(top_k * 4, 12)
        rankings: list[tuple[list[ScoredChunk], float]] = [
            (self.sparse.search(query, top_k=pool), 1.0)
        ]
        if self.dense is not None:
            try:
                rankings.append(
                    (self.dense.search(query, top_k=pool), self.dense_weight)
                )
            except Exception as exc:  # 降级而不是崩
                self.last_degraded = f"{type(exc).__name__}: {exc}"

        # ⚠️ 融合键必须按**内容**，不能按 chunk.id：
        # BM25 侧的 id 是 `{source}#N`、向量侧是 `{instance_id}#N`，
        # 同一篇文档在两个 store 里的 id 永不相等 —— 按 id 融合，
        # 两路互证永远不会发生，同一文档还会以两个 id 各占一个
        # top_k 名额、在 prompt 里重复出现（2026-09-22 实测复现）。
        # (source, heading, text) 三元组才能唯一标识「同一篇规范」。
        fused: dict[tuple[str, str, str], float] = {}
        by_key: dict[tuple[str, str, str], Chunk] = {}
        for hits, weight in rankings:
            for rank, h in enumerate(hits):
                key = (h.chunk.source, h.chunk.heading, h.chunk.text)
                by_key.setdefault(key, h.chunk)
                fused[key] = fused.get(key, 0.0) + weight / (self.rrf_k + rank + 1)

        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
        return [ScoredChunk(chunk=by_key[k], score=round(s, 8)) for k, s in ranked]

    def render_context(self, query: str, top_k: int = 3, limit: int = 1200) -> str:
        return _render(self.search(query, top_k=top_k), limit)

    def __len__(self) -> int:
        return len(self.sparse)

    def stats(self) -> dict:
        d = self.sparse.stats() if hasattr(self.sparse, "stats") else {}
        d["fusion"] = "rrf"
        d["rrf_k"] = self.rrf_k
        d["dense"] = self.dense.stats() if hasattr(self.dense, "stats") else None
        if self.last_degraded:
            d["degraded"] = self.last_degraded
        return d


def _render(hits: list[ScoredChunk], limit: int) -> str:
    """与 ConventionStore.render_context 保持一致的拼装口径。"""
    if not hits:
        return ""
    parts: list[str] = []
    used = 0
    for h in hits:
        head = f"[{h.chunk.heading or h.chunk.source}]"
        body = h.chunk.brief(400)
        block = f"{head}\n{body}"
        if used + len(block) > limit:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def build_store(
    paths: Iterable[str] = (),
    inline: Iterable[str] = (),
    *,
    backend: str = "bm25",
    embedder: Embedder | None = None,
    collection_name: str = "conventions",
) -> Retriever:
    """按 backend 构造检索器。

    backend:
      - `bm25`   ：纯标准库 BM25（默认，零依赖、CI 最快）
      - `vector` ：仅 Chroma 向量检索
      - `hybrid` ：BM25 + 向量，RRF 融合

    返回类型都满足 `Retriever` 协议，reviewer.py 无需感知差异。
    """
    backend = (backend or "bm25").lower()

    if backend == "bm25":
        s = ConventionStore()
        _fill(s, paths, inline)
        return s

    if backend == "vector":
        v = ChromaVectorRetriever(embedder=embedder, collection_name=collection_name)
        _fill(v, paths, inline)
        return v

    if backend == "hybrid":
        s = ConventionStore()
        _fill(s, paths, inline)
        v = ChromaVectorRetriever(
            embedder=embedder, collection_name=collection_name + "-vec"
        )
        _fill(v, paths, inline)
        return HybridRetriever(sparse=s, dense=v)

    raise ValueError(f"未知 backend: {backend}（可选 bm25 / vector / hybrid）")


def _fill(store, paths: Iterable[str], inline: Iterable[str]) -> None:
    for t in inline:
        store.add_text(t, source="inline")
    for p in paths:
        try:
            store.add_file(p)
        except OSError:
            continue
