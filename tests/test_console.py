"""控制台编码回归测试。

背景：Windows 上 stdout 是 cp936/GBK，报告里的 ✓ / ✗ 编不出来，
评测脚本在打印最后一行时抛 UnicodeEncodeError 崩掉 —— 数据全对，
只是打不出来，但现象看着像评测本身失败。

所以这里最重要的不是断言函数返回值，而是把子进程的 stdio 钉成 gbk
跑一遍真流程，确认它不再崩。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pagent.console import ensure_utf8_stdio  # noqa: E402


def test_tolerates_stream_without_reconfigure():
    """StringIO 没有 reconfigure —— 只能返回 False，不能抛异常。"""
    old = sys.stdout
    try:
        sys.stdout = io.StringIO()
        assert ensure_utf8_stdio() is False
    finally:
        sys.stdout = old


def test_reconfigured_stream_still_works():
    raw = io.BytesIO()
    buf = io.TextIOWrapper(raw, encoding="utf-8")
    old = sys.stdout
    try:
        sys.stdout = buf
        assert ensure_utf8_stdio() is True
        buf.write("✓ ✗")
        buf.flush()
    finally:
        sys.stdout = old
    assert raw.getvalue().decode("utf-8") == "✓ ✗"


def test_never_raises_on_closed_stream():
    old = sys.stdout
    try:
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        buf.close()
        sys.stdout = buf
        assert ensure_utf8_stdio() is False  # 不抛
    finally:
        sys.stdout = old


def test_eval_survives_gbk_console():
    """真回归：PYTHONIOENCODING=gbk 时，评测脚本必须跑完且退出码为 0。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "eval" / "run_eval.py"), "--mode", "rules"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(ROOT), timeout=300,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "UnicodeEncodeError" not in combined, combined[-2000:]
    assert proc.returncode == 0, combined[-2000:]
    # 报告确实打印到了带 ✓/✗ 的汇总区
    assert "规则召回率" in combined or "召回" in combined, combined[-2000:]
