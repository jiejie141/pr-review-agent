"""LLM 客户端与离线替身测试。

重点测「模型输出很脏」这个前提：解析器必须容错，而不是抛异常让整次审查失败。
"""

import json

import pytest

from pagent.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    MockLLMClient,
    estimate_tokens,
    parse_json_payload,
)


# ------------------------------------------------------------ JSON 容错解析
def test_parse_plain_json_object():
    assert parse_json_payload('{"findings": []}') == {"findings": []}


def test_parse_plain_json_array():
    assert parse_json_payload('[1, 2, 3]') == [1, 2, 3]


def test_parse_strips_markdown_fence():
    text = '```json\n{"findings": [{"line": 3}]}\n```'
    assert parse_json_payload(text) == {"findings": [{"line": 3}]}


def test_parse_strips_fence_without_language_tag():
    assert parse_json_payload('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_extracts_from_prose():
    text = '好的，我审查完了。\n\n{"findings": [{"line": 9}]}\n\n希望有帮助。'
    assert parse_json_payload(text) == {"findings": [{"line": 9}]}


def test_parse_repairs_trailing_comma():
    assert parse_json_payload('{"findings": [{"line": 1},]}') == {"findings": [{"line": 1}]}


def test_parse_picks_earliest_container():
    """最早出现的容器才是最外层。固定优先数组会把 {"findings": [...]} 截错。"""
    got = parse_json_payload('说明 {"x": 1} 然后是 [1,2] 结束')
    assert got == {"x": 1}

    got2 = parse_json_payload('[1,2] 前面没有对象')
    assert got2 == [1, 2]


def test_parse_raises_on_garbage():
    with pytest.raises(LLMError):
        parse_json_payload("完全不是 JSON 的一段话")


def test_parse_raises_on_empty():
    with pytest.raises(LLMError):
        parse_json_payload("")
    with pytest.raises(LLMError):
        parse_json_payload("   ")


# ------------------------------------------------------------ token 估算
def test_estimate_tokens_zero_for_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_scales_with_length():
    short = estimate_tokens("hello")
    long = estimate_tokens("hello" * 100)
    assert long > short


def test_estimate_tokens_counts_cjk_higher():
    assert estimate_tokens("中文中文中文中文") > estimate_tokens("abcd")


# ------------------------------------------------------------ LLMClient
def test_client_requires_api_key():
    with pytest.raises(LLMError):
        LLMClient(api_key="")


def test_client_normalizes_base_url():
    c = LLMClient(api_key="x" * 30, base_url="https://api.example.com/v1/")
    assert c.base_url == "https://api.example.com/v1"


def test_client_starts_with_json_mode_enabled():
    assert LLMClient(api_key="x" * 30).supports_json_mode is True


def test_unsupported_format_detection():
    assert LLMClient._looks_like_unsupported_format('{"error":"response_format is not supported"}')
    assert LLMClient._looks_like_unsupported_format('"json_object" unsupported')
    assert not LLMClient._looks_like_unsupported_format('{"error":"invalid api key"}')


def test_response_total_tokens():
    r = LLMResponse(prompt_tokens=10, completion_tokens=5)
    assert r.total_tokens == 15


# ------------------------------------------------------------ 离线替身
def test_mock_returns_valid_json_in_expected_shape():
    mock = MockLLMClient()
    resp = mock.chat([{"role": "user", "content": "12: +def f(order_id):\n13: +    return order_id"}])
    payload = json.loads(resp.text)
    assert "findings" in payload
    assert isinstance(payload["findings"], list)


def test_mock_counts_calls_and_tokens():
    mock = MockLLMClient()
    resp = mock.chat([{"role": "user", "content": "hi"}])
    assert mock.request_count == 1
    assert resp.prompt_tokens > 0


def test_mock_flags_missing_parameter_validation():
    prompt = "12: +def process(order_id, amount):\n13: +    return order_id\n14: +    return amount"
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    titles = [f["title"] for f in out["findings"]]
    assert any("边界校验" in t for t in titles)


def test_mock_skips_parameter_validation_when_guard_present():
    prompt = (
        "12: +def process(order_id):\n"
        "13: +    if order_id is None:\n"
        "14: +        raise ValueError('bad')\n"
    )
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    assert not any("边界校验" in f["title"] for f in out["findings"])


def test_mock_requires_two_writes_for_transaction_finding():
    one = "12: +    repo.save(order)\n13: +    return order"
    out1 = json.loads(MockLLMClient().chat([{"role": "user", "content": one}]).text)
    assert not any("事务" in f["title"] for f in out1["findings"])

    two = "12: +    repo.save(order)\n13: +    ledger.insert(order)\n14: +    return order"
    out2 = json.loads(MockLLMClient().chat([{"role": "user", "content": two}]).text)
    assert any("事务" in f["title"] for f in out2["findings"])


def test_mock_skips_full_line_comments():
    prompt = "12: +    # 这段代码没有 timeout，也没有 try\n13: +    # def process(order_id): 只是注释"
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    assert out["findings"] == []


def test_mock_flags_external_request_without_timeout():
    prompt = "12: +    requests.post(callback_url, json=payload)"
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    assert any("超时" in f["title"] for f in out["findings"])


def test_mock_accepts_external_request_with_timeout():
    prompt = (
        "12: +    try:\n"
        "13: +        return requests.post(callback_url, json=payload, timeout=3)\n"
        "14: +    except requests.Timeout:\n"
        "15: +        return None\n"
    )
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    assert not any("超时" in f["title"] for f in out["findings"])


def test_mock_emits_only_valid_line_numbers_from_input():
    prompt = "12: +def process(order_id):\n13: +    return order_id"
    out = json.loads(MockLLMClient().chat([{"role": "user", "content": prompt}]).text)
    assert all(f["line"] in (12, 13) for f in out["findings"])


def test_mock_records_prompts_for_inspection():
    mock = MockLLMClient()
    mock.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert mock.calls[0][0]["content"] == "s"


def test_mock_chat_json_returns_object():
    got = MockLLMClient().chat_json([{"role": "user", "content": "1: +x = 1"}])
    assert isinstance(got, dict) and "findings" in got
