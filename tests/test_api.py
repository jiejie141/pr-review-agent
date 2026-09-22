"""FastAPI 服务层单测（pr-review-agent）。

全部走 mock，不联网、不消耗 token —— 单测不该有外部依赖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pagent.api import app

DEMO_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,5 +1,9 @@
 import os
+import hashlib
+
+DB_PASSWORD = "hunter2_super_secret_2024"
+
 def get_user(uid):
-    return db.query(uid)
+    sql = f"SELECT * FROM users WHERE id = {uid}"
+    return db.execute(sql)
"""


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok"
    assert b["retrieval_backend"] in ("bm25", "vector", "hybrid")
    assert b["require_line_anchor"] is True


def test_backends_lists_bm25_always(client):
    r = client.get("/backends")
    assert r.status_code == 200
    assert "bm25" in r.json()["available"]


def test_openapi_schema_generated(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "pr-review-agent API"
    assert "/review" in spec["paths"]


def test_console_page_is_served(client):
    """GET / 返回审查控制台 HTML。

    顺带守住部署问题：页面按包内相对路径找（src/pagent/web/index.html），
    一旦打包时漏掉这个目录，这里会立刻红。
    """
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert body.lstrip().lower().startswith("<!doctype html")
    assert "pr-review-agent" in body
    for anchor in ('id="diff"', 'id="go"', 'id="findings"', 'id="backend"'):
        assert anchor in body, f"控制台缺少锚点 {anchor}"


def test_console_not_in_openapi(client):
    """控制台是页面不是接口，不该出现在 OpenAPI schema 里。"""
    spec = client.get("/openapi.json").json()
    assert "/" not in spec["paths"]


def test_review_finds_hardcoded_secret(client):
    r = client.post("/review", json={"diff": DEMO_DIFF, "mock": True})
    assert r.status_code == 200
    b = r.json()
    assert b["findings"], "应至少命中硬编码密钥或 SQL 注入"
    ids = {f["rule_id"] for f in b["findings"]}
    assert any(i.startswith("SEC") for i in ids), f"应有安全类规则命中，实际: {ids}"


def test_review_response_shape(client):
    r = client.post("/review", json={"diff": DEMO_DIFF, "mock": True})
    f = r.json()["findings"][0]
    for k in ("rule_id", "title", "category", "severity", "file", "line",
              "source", "anchors_ok"):
        assert k in f, k
    assert f["source"] in ("rule", "llm")
    assert f["severity"] in ("high", "medium", "low", "info")


def test_review_rejects_empty_diff(client):
    r = client.post("/review", json={"diff": ""})
    assert r.status_code == 422


def test_review_rejects_bad_backend(client):
    r = client.post(
        "/review", json={"diff": DEMO_DIFF, "mock": True, "retrieval_backend": "nope"}
    )
    assert r.status_code == 400
    assert "未知检索后端" in r.json()["detail"]


@pytest.mark.parametrize("backend", ["bm25", "vector", "hybrid"])
def test_review_works_on_every_backend(client, backend):
    """三种检索后端都必须能跑完整条链路 —— 接口契约一致。"""
    r = client.post(
        "/review", json={"diff": DEMO_DIFF, "mock": True, "retrieval_backend": backend}
    )
    assert r.status_code == 200, r.text
    assert r.json()["findings"], backend


def test_backend_override_does_not_leak_to_global_settings(client):
    """按请求覆盖检索后端，不得污染 get_settings() 单例。

    钉住 2026-09-22 审查发现的问题：旧实现直接给单例字段赋值，
    一次 retrieval_backend=hybrid 会把后续所有请求和 /health 的
    显示一起带走。
    """
    from pagent.config import get_settings

    before = get_settings().retrieval_backend
    r = client.post(
        "/review",
        json={"diff": DEMO_DIFF, "mock": True,
              "retrieval_backend": "hybrid" if before != "hybrid" else "bm25"},
    )
    assert r.status_code == 200, r.text
    assert get_settings().retrieval_backend == before
    h = client.get("/health")
    assert h.json()["retrieval_backend"] == before


def test_review_stats_populated(client):
    r = client.post("/review", json={"diff": DEMO_DIFF, "mock": True})
    st = r.json()["stats"]
    assert st.get("files", 0) >= 1
    assert st.get("rule_findings", 0) >= 1


# --- mode 三态：这一组是"花钱开关"的回归防线 -------------------------------
# 背景：旧的两个布尔 mock / use_llm 是相乘语义，默认值 (True, True)
# 组合出来的是**真实付费调用**，而字段名写着"离线替身"。下面这几条断言
# 就是为了让"默认绝不发网络请求"这件事再也回退不了。


@pytest.mark.parametrize("payload", [{}, {"mode": "rules"}])
def test_default_never_calls_llm(client, payload):
    """不给 mode / 给 rules：模型调用次数必须是 0。"""
    r = client.post("/review", json={"diff": DEMO_DIFF, **payload})
    assert r.status_code == 200, r.text
    st = r.json()["stats"]
    assert st["mode"] == "rules"
    assert st["llm_calls"] == 0
    assert st["total_tokens"] == 0
    assert st["rule_findings"] >= 1, "规则层必须照样出货"


def test_mock_mode_runs_semantic_layer_without_network(client):
    """mock 模式：语义层真的跑（llm_calls>0），但统计里 mode 标明是替身。

    这里不直接断言"没联网"（单测环境本来就联不了网），而是断言
    结果**自证身份**：调用方拿到报告时必须能一眼看出这不是真实模型结论。
    """
    r = client.post("/review", json={"diff": DEMO_DIFF, "mode": "mock"})
    assert r.status_code == 200, r.text
    st = r.json()["stats"]
    assert st["mode"] == "mock"
    assert st["llm_calls"] >= 1


def test_live_mode_without_key_is_a_clear_400(client, monkeypatch):
    """要真实调用但没配 Key：必须报错，绝不能静默降级成替身。

    静默降级最坏的地方在于报告里看不出差别 —— 用户会拿一份离线替身的
    结果当成真实模型结论去决定是否合并代码。

    注意这里改的是 `get_settings()` 返回的**单例**：接口内部用的也是这个
    实例，所以 patch 它才是有效的（曾试过 patch 一个临时构造的 Settings，
    结果接口读到的仍是带 Key 的单例，断言直接失败）。
    """
    from pagent.api import get_settings

    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    r = client.post("/review", json={"diff": DEMO_DIFF, "mode": "live"})
    assert r.status_code == 400
    assert "LLM_API_KEY" in r.json()["detail"]


def test_legacy_flags_map_to_safe_mode(client):
    """旧的 mock / use_llm 仍可用，但语义要映射到安全的一态。

    关键用例是 `mock=True`：调用方写这个词的本意几乎一定是"别真的调模型"，
    所以它必须落到 mock（零网络）而不是 live。
    """
    r = client.post("/review", json={"diff": DEMO_DIFF, "mock": True})
    assert r.json()["stats"]["mode"] == "mock"

    r = client.post("/review", json={"diff": DEMO_DIFF, "use_llm": False})
    st = r.json()["stats"]
    assert st["mode"] == "rules"
    assert st["llm_calls"] == 0


def test_bad_mode_rejected(client):
    r = client.post("/review", json={"diff": DEMO_DIFF, "mode": "yolo"})
    assert r.status_code == 422
