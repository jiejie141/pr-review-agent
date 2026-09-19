"""控制台编码兜底。

Windows 上 stdout 默认走本地代码页（简体中文是 cp936/GBK），而报告里要打印
✓ / ✗ 这类符号 —— GBK 编不出来，于是 `print()` 抛 UnicodeEncodeError，
整个命令崩在最后一步：数据全算对了，只是打不出来。

现象很误导（看着像评测失败），根因却在编码。所以每个进程入口调用一次
ensure_utf8_stdio()。

这里刻意不抛异常：拿不到可重配置的流（测试里的 StringIO，或被管道接走的
旧解释器）就静默跳过 —— 编码兜底失败不该让主流程失败。
"""

from __future__ import annotations

import sys


def ensure_utf8_stdio() -> bool:
    """尽力把 stdout/stderr 切到 UTF-8，返回是否全部成功。"""
    ok = True
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            ok = False
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 流已关闭，或已被上游接管
            ok = False
    return ok
