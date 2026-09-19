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


def test_review_stats_populated(client):
    r = client.post("/review", json={"diff": DEMO_DIFF, "mock": True})
    st = r.json()["stats"]
    assert st.get("files", 0) >= 1
    assert st.get("rule_findings", 0) >= 1
