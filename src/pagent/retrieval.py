"""仓库规范检索（RAG 的检索侧）。

为什么自己写而不是直接上向量库：
1. 这个场景的语料很小 —— 一个仓库的规范文档通常几千字，BM25 完全够用；
2. 零第三方依赖，CI 里能离线跑，评审者 clone 下来就能复现；
3. 中文 + 英文混排的规范文档，通用分词器反而不如「中文按字二元组」稳。

接口留了 `Retriever` 协议，想换成 bge + Chroma 只要实现同一个 search 即可。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """英文按词、中文按字二元组。

    中文没有空格，按单字切会丢掉大部分区分度（「异常」和「异样」都含「异」），
    二元组能把「异常」保留为一个特征，代价只是索引稍大。
    """
    text = text.lower()
    tokens: list[str] = [m.group(0) for m in _ASCII_WORD.finditer(text)]
    # 只保留连续中文片段内的相邻二元组
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        seg = m.group(0)
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


@dataclass
class Chunk:
    id: str
    text: str
    source: str = ""
    heading: str = ""
    tokens: list[str] = field(default_factory=list)

    def brief(self, limit: int = 300) -> str:
        t = self.text.strip()
        return t if len(t) <= limit else t[:limit] + "…"


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float


class Retriever(Protocol):
    def search(self, query: str, top_k: int = 3) -> list[ScoredChunk]: ...


class ConventionStore:
    """BM25 检索器。参数取的是常规默认值（k1=1.5, b=0.75）。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75, min_chunk_chars: int = 24) -> None:
        self.k1 = k1
        self.b = b
        self.min_chunk_chars = min_chunk_chars
        self.chunks: list[Chunk] = []
        self._df: dict[str, int] = {}
        self._avg_len: float = 0.0

    # -- 索引 --------------------------------------------------------------
    def add_text(self, text: str, source: str = "inline") -> int:
        """按 Markdown 标题 / 空行切块。返回新增块数。"""
        added = 0
        heading = ""
        buf: list[str] = []

        def flush() -> None:
            nonlocal buf, added
            body = "\n".join(buf).strip()
            buf = []
            if len(body) < self.min_chunk_chars:
                return
            cid = f"{source}#{len(self.chunks)}"
            # 标题一并入索引：规范文档里「异常处理」「密钥与配置」这类章节名
            # 本身就是最强的检索信号，只索引正文会让这类查询全部落空
            indexable = f"{heading}\n{body}" if heading else body
            self.chunks.append(
                Chunk(
                    id=cid,
                    text=body,
                    source=source,
                    heading=heading,
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
        self._reindex()
        return added

    def add_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return self.add_text(fh.read(), source=path)

    def add_files(self, paths: Iterable[str]) -> int:
        return sum(self.add_file(p) for p in paths)

    def _reindex(self) -> None:
        self._df = {}
        for c in self.chunks:
            for t in set(c.tokens):
                self._df[t] = self._df.get(t, 0) + 1
        total = sum(len(c.tokens) for c in self.chunks)
        self._avg_len = total / len(self.chunks) if self.chunks else 0.0

    # -- 检索 --------------------------------------------------------------
    def search(self, query: str, top_k: int = 3) -> list[ScoredChunk]:
        if not self.chunks:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        n = len(self.chunks)
        scored: list[ScoredChunk] = []
        for c in self.chunks:
            score = self._bm25(q_tokens, c, n)
            if score > 0:
                scored.append(ScoredChunk(chunk=c, score=score))
        scored.sort(key=lambda s: -s.score)
        return scored[:top_k]

    def _bm25(self, q_tokens: list[str], c: Chunk, n: int) -> float:
        tf: dict[str, int] = {}
        for t in c.tokens:
            tf[t] = tf.get(t, 0) + 1
        dl = len(c.tokens) or 1
        score = 0.0
        for t in q_tokens:
            if t not in tf:
                continue
            df = self._df.get(t, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            num = tf[t] * (self.k1 + 1)
            den = tf[t] + self.k1 * (1 - self.b + self.b * dl / (self._avg_len or 1))
            score += idf * num / den
        return score

    def render_context(self, query: str, top_k: int = 3, limit: int = 1200) -> str:
        """拼成可直接塞进提示词的文本块。检索为空时返回空串，
        让上层明确知道「这个仓库没有相关约定」，而不是塞一段无关内容进去。"""
        hits = self.search(query, top_k=top_k)
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

    # -- 自省 --------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.chunks)

    def stats(self) -> dict:
        return {
            "chunks": len(self.chunks),
            "vocab": len(self._df),
            "avg_tokens": round(self._avg_len, 1),
            "sources": sorted({c.source for c in self.chunks}),
        }


def build_bm25_store(paths: Iterable[str] = (), inline: Iterable[str] = ()) -> ConventionStore:
    """构造纯 BM25 检索器。

    ⚠️ 这个名字**曾经叫 build_store**，与 vector_store.build_store（带
    backend 参数的那个）同名不同签名 —— 两处各存一份，import 时极易拿错
    （reviewer 能跑对全靠"从 vector_store 拿"这个约定）。2026-09-22
    审查后改名，构造入口统一收在 vector_store.build_store。"""
    store = ConventionStore()
    for t in inline:
        store.add_text(t, source="inline")
    for p in paths:
        try:
            store.add_file(p)
        except OSError:
            continue
    return store
