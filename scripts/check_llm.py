"""LLM 连通性检查。

单独拿出来是因为排障时最想确认的就是「Key 和 Base URL 到底通不通」，
而不想跑一整套审查流程。

    python scripts/check_llm.py
    python scripts/check_llm.py --model deepseek-chat
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pagent.config import get_settings  # noqa: E402
from pagent.console import ensure_utf8_stdio  # noqa: E402
from pagent.llm import LLMClient, LLMError, estimate_tokens  # noqa: E402

PROBE = "回复两个字：可用"


def main() -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="检查 LLM 配置是否可用")
    ap.add_argument("--model", help="临时覆盖模型名")
    ap.add_argument("--base-url", help="临时覆盖 Base URL")
    ap.add_argument("--json", action="store_true", help="顺带验证 json_mode 是否被支持")
    args = ap.parse_args()

    st = get_settings(reload=True)
    key = st.llm_api_key
    base = (args.base_url or st.llm_base_url).rstrip("/")
    model = args.model or st.llm_model

    print("=== LLM 连通性检查 ===")
    print(f"Base URL : {base}")
    print(f"模型     : {model}")
    print(f"Key      : {'已配置（' + str(len(key)) + ' 字符）' if key else '未配置'}")
    print("")

    if not key:
        print("✗ 未配置 LLM_API_KEY。")
        print("  在项目根目录创建 .env，写入 LLM_API_KEY=...（可从 .env.example 复制）")
        print("  或者用离线替身跑通流程：python main.py --diff examples/demo.patch --mock")
        return 1

    client = LLMClient(
        api_key=key,
        base_url=base,
        model=model,
        timeout=st.llm_timeout,
        max_retries=1,
    )
    try:
        resp = client.chat([{"role": "user", "content": PROBE}], temperature=0)
    except LLMError as e:
        print(f"✗ 调用失败：{e}")
        print("")
        _hint(str(e))
        return 1

    print(f"✓ 连通成功")
    print(f"  返回    : {resp.text.strip()[:60]}")
    print(f"  实际模型: {resp.model}")
    print(f"  用量    : 输入 {resp.prompt_tokens} / 输出 {resp.completion_tokens} token")

    if args.json:
        print("")
        print("--- json_mode 探测 ---")
        try:
            got = client.chat_json([{"role": "user", "content": '输出 JSON：{"ok": true}'}])
            print(f"✓ 支持（原始模式={client.supports_json_mode}）：{got}")
        except LLMError as e:
            print(f"✗ 失败：{e}")

    print("")
    print("估算：单次 PR 审查通常消耗 3k~15k token。")
    print(f"参考：本项目的系统提示词约 {estimate_tokens(open(ROOT / 'src' / 'pagent' / 'reviewer.py', encoding='utf-8').read())} token 量级")
    return 0


def _hint(err: str) -> None:
    low = err.lower()
    if "401" in err:
        print("  401 = Key 无效或已过期。检查：")
        print("    1) .env 里有没有多余空格或引号")
        print("    2) Key 是否属于当前 Base URL 对应的供应商（DeepSeek 的 Key 不能用于 Moonshot）")
        print("    3) Key 是否已被吊销")
    elif "403" in err:
        print("  403 = 权限不足。可能是 Key 未开通该模型，或账号欠费。")
    elif "404" in err:
        print("  404 = Base URL 或模型名不对。注意 Base URL 通常要带 /v1。")
    elif "429" in err:
        print("  429 = 触发限流。稍后重试，或换用更小的模型。")
    elif "timeout" in low or "网络" in err:
        print("  网络问题。检查代理设置，或把 LLM_TIMEOUT 调大。")


if __name__ == "__main__":
    raise SystemExit(main())
