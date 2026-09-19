"""项目入口。

把 src/ 加进 sys.path 后转交给 pagent.cli，这样在仓库根目录直接
`python main.py ...` 就能跑，不需要先 pip install -e .。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pagent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
