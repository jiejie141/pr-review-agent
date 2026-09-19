"""仓库规范检索测试。"""

from pagent.retrieval import ConventionStore, build_store, tokenize

DOC = """# 后端代码规范

## 命名

模块与包用小写下划线，类用大驼峰，常量全大写。
禁止单字母变量名。

## 异常处理

禁止裸 except，禁止 except Exception: pass。
捕获后必须记录日志或向上抛出。

## 资源管理

文件与连接一律用 with 管理，禁止裸 open 后手动 close。
"""

SEC = """# 安全编码基线

## 输入与查询

所有 SQL 必须参数化，禁止用 f-string 或 % 拼接查询语句。

## 密钥与配置

密钥一律来自环境变量，禁止硬编码。仓库内只允许存在 .env.example。
"""


def test_tokenize_handles_mixed_language():
    tokens = tokenize("禁止裸 except 语句")
    assert "except" in tokens
    assert "禁止" in tokens
    assert "止裸" in tokens  # 中文二元组，保证「异常」这类词有区分度


def test_tokenize_is_case_insensitive():
    assert "select" in tokenize("SELECT * FROM t")


def test_chunking_splits_by_heading_and_blank_line():
    store = ConventionStore()
    n = store.add_text(DOC, source="style.md")
    assert n >= 3
    assert all(c.tokens for c in store.chunks)


def test_short_fragments_are_dropped():
    store = ConventionStore(min_chunk_chars=24)
    store.add_text("# T\n\nok\n\n这是一段足够长的中文说明用来测试切块行为。", source="x")
    assert all(len(c.text) >= 24 for c in store.chunks)


def test_search_returns_relevant_chunk_first():
    store = ConventionStore()
    store.add_text(DOC + "\n\n" + SEC, source="docs")
    hits = store.search("SQL 参数化 拼接", top_k=3)
    assert hits
    assert "SQL" in hits[0].chunk.text or "参数化" in hits[0].chunk.text


def test_search_finds_exception_section():
    store = ConventionStore()
    store.add_text(DOC, source="style.md")
    hits = store.search("裸 except 吞异常", top_k=2)
    assert hits
    assert "except" in hits[0].chunk.text


def test_search_on_empty_store_returns_empty():
    assert ConventionStore().search("任意查询") == []


def test_search_with_unmatched_query_returns_empty():
    store = ConventionStore()
    store.add_text(DOC, source="d")
    assert store.search("量子计算 拓扑绝缘体 超导") == []


def test_render_context_includes_heading():
    store = ConventionStore()
    store.add_text(DOC, source="style.md")
    ctx = store.render_context("异常处理", top_k=1)
    assert ctx
    assert ctx.strip()


def test_render_context_respects_limit():
    store = ConventionStore()
    store.add_text(DOC * 5, source="d")
    ctx = store.render_context("规范", top_k=5, limit=200)
    assert len(ctx) <= 250


def test_render_context_empty_when_no_match():
    store = ConventionStore()
    store.add_text(DOC, source="d")
    assert store.render_context("完全无关的主题") == ""


def test_stats_reports_sources():
    store = ConventionStore()
    store.add_text(DOC, source="a.md")
    store.add_text(SEC, source="b.md")
    st = store.stats()
    assert st["chunks"] == len(store.chunks)
    assert st["sources"] == ["a.md", "b.md"]
    assert st["vocab"] > 0


def test_build_store_from_real_files():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    paths = sorted((root / "examples" / "conventions").glob("*.md"))
    assert paths, "examples/conventions 下应有规范文档"
    store = build_store([str(p) for p in paths])
    assert len(store) > 10
    hits = store.search("参数化查询", top_k=1)
    assert hits


def test_build_store_ignores_missing_files():
    store = build_store(
        ["/definitely/not/here.md"],
        inline=["这是一段足够长的内联中文规范文本，用于确认缺失文件被安全跳过。"],
    )
    assert len(store) == 1


def test_bm25_favors_rarer_terms():
    store = ConventionStore()
    store.add_text(DOC, source="style")
    store.add_text(SEC, source="sec")
    a = store.search("密钥", top_k=2)
    b = store.search("变量名", top_k=2)
    assert a and b
    assert "密钥" in a[0].chunk.text
    assert "变量名" in b[0].chunk.text
