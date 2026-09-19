"""pytest 公共配置：把 src/ 挂进 import 路径。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402


@pytest.fixture
def simple_patch() -> str:
    return (
        "diff --git a/app/example.py b/app/example.py\n"
        "--- a/app/example.py\n"
        "+++ b/app/example.py\n"
        "@@ -1,3 +1,6 @@\n"
        " import logging\n"
        " \n"
        " \n"
        "+def add(a, b):\n"
        "+    return a + b\n"
        "+\n"
    )


@pytest.fixture
def vulnerable_patch() -> str:
    return (
        "diff --git a/app/db.py b/app/db.py\n"
        "--- a/app/db.py\n"
        "+++ b/app/db.py\n"
        "@@ -1,2 +1,6 @@\n"
        " import sqlite3\n"
        " \n"
        '+DB_PASSWORD = "hunter2_super_secret_2024"\n'
        "+\n"
        "+def find_user(username):\n"
        '+    cur.execute("SELECT * FROM u WHERE n = \'" + username + "\'")\n'
    )


@pytest.fixture
def clean_patch() -> str:
    return (
        "diff --git a/app/safe.py b/app/safe.py\n"
        "--- a/app/safe.py\n"
        "+++ b/app/safe.py\n"
        "@@ -1,2 +1,6 @@\n"
        " import sqlite3\n"
        " \n"
        "+def find_user(conn, username):\n"
        '+    with open("/tmp/x", "r", encoding="utf-8") as fh:\n'
        "+        return fh.read()\n"
    )
