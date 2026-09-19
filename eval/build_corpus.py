"""生成评测语料。

为什么用 difflib 而不是 git：
`git diff --no-index` 需要两棵真实目录树，且路径前缀会污染文件名。
difflib 是标准库，输出同样是合法 unified diff，且路径完全可控。

评测口径说明（很重要，别把数字读错）：
- `expect_rules` 是**标注好的、确定存在的缺陷**，用于算召回率。
- `negative=True` 的用例是**干净的改动**，在这里产生的任何意见都算误报。
- `expect_semantic` 是规则库抓不到、只能靠模型理解的缺陷，单独统计。
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "eval" / "corpus"
LABELS = ROOT / "eval" / "labels.json"

HEAD = '"""业务模块。"""\n\nimport logging\n\nlogger = logging.getLogger(__name__)\n\n\n'


def mk(files: dict[str, tuple[str, str]]) -> str:
    """files: {路径: (改动前, 改动后)} → unified diff 文本"""
    out: list[str] = []
    for path, (before, after) in files.items():
        b = before.splitlines(keepends=True)
        a = after.splitlines(keepends=True)
        if b and not b[-1].endswith("\n"):
            b[-1] += "\n"
        if a and not a[-1].endswith("\n"):
            a[-1] += "\n"
        body = "".join(
            difflib.unified_diff(b, a, fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
        )
        if not body:
            continue
        out.append(f"diff --git a/{path} b/{path}\n")
        out.append(body)
    return "".join(out)


CASES: list[dict] = []


def case(cid, title, files, expect_rules=(), expect_semantic=(), negative=False, note="", heldout=False):
    CASES.append(
        {
            "id": cid,
            "title": title,
            "diff": mk(files),
            "expect_rules": list(expect_rules),
            "expect_semantic": list(expect_semantic),
            "negative": negative,
            "note": note,
            "heldout": heldout,
        }
    )


# ==========================================================================
# 安全维度
# ==========================================================================
case(
    "c01_sql_and_secret",
    "SQL 注入 + 硬编码凭据",
    {
        "app/db.py": (
            HEAD + 'DB_URL = "sqlite:///app.db"\n\n\ndef health():\n    return True\n',
            HEAD
            + 'DB_URL = "sqlite:///app.db"\n'
            + 'DB_PASSWORD = "hunter2_super_secret_2024"\n\n\n'
            + "def health():\n    return True\n\n\n"
            + "def find_user(username):\n"
            + '    cur = get_conn().cursor()\n'
            + "    cur.execute(\"SELECT * FROM users WHERE name = '\" + username + \"'\")\n"
            + "    return cur.fetchone()\n\n\n"
            + "def delete_user(uid):\n"
            + "    conn = get_conn()\n"
            + '    conn.execute("DELETE FROM users WHERE id = %s" % uid)\n'
            + "    conn.commit()\n",
        )
    },
    expect_rules=["SEC101", "SEC103"],
    note="拼接 SQL 的两种常见写法都覆盖：+ 拼接与 % 格式化。",
)

case(
    "c02_command_injection",
    "命令注入 + 调试打印残留",
    {
        "app/report.py": (
            HEAD + "def build(name):\n    return name\n",
            HEAD
            + "def build(name):\n    return name\n\n\n"
            + "def render_report(name):\n"
            + '    cmd = "convert %s /tmp/out.pdf" % name\n'
            + "    os.system(cmd)\n"
            + '    print("rendered:", name)\n'
            + "    return cmd\n",
        )
    },
    expect_rules=["SEC102", "CONV303"],
)

case(
    "c03_deserialization",
    "不安全反序列化 + 动态执行",
    {
        "app/cache.py": (
            HEAD + "def key_of(name):\n    return name\n",
            HEAD
            + "def key_of(name):\n    return name\n\n\n"
            + "def load_blob(raw):\n"
            + "    return pickle.loads(raw)\n\n\n"
            + "def run_rule(expr, ctx):\n"
            + "    return eval(expr, {}, ctx)\n",
        )
    },
    expect_rules=["SEC105", "SEC104"],
)

case(
    "c04_tls_and_hash",
    "关闭 TLS 校验 + 弱哈希存口令",
    {
        "app/auth.py": (
            HEAD + "def current_user():\n    return None\n",
            HEAD
            + "def current_user():\n    return None\n\n\n"
            + "def fetch_profile(url):\n"
            + "    return requests.get(url, verify=False).json()\n\n\n"
            + "def hash_password(password):\n"
            + "    return hashlib.md5(password.encode()).hexdigest()\n",
        )
    },
    expect_rules=["SEC106", "SEC107"],
)

case(
    "c05_ssrf_and_path",
    "SSRF + 路径穿越",
    {
        "app/files.py": (
            HEAD + "BASE_DIR = '/srv/data'\n\n\ndef ping():\n    return 'pong'\n",
            HEAD
            + "BASE_DIR = '/srv/data'\n\n\n"
            + "def ping():\n    return 'pong'\n\n\n"
            + "def proxy(callback_url):\n"
            + "    return requests.get(callback_url).content\n\n\n"
            + "def read_upload(filename):\n"
            + "    return open(os.path.join(BASE_DIR, filename)).read()\n",
        )
    },
    expect_rules=["SEC108", "SEC109"],
)

case(
    "c06_debug_and_log",
    "生产开启调试 + 敏感信息入日志",
    {
        "app/server.py": (
            HEAD + "app = create_app()\n",
            HEAD
            + "app = create_app()\n\n\n"
            + "def login(username, password):\n"
            + '    logger.info("login attempt user=%s password=%s", username, password)\n'
            + "    return True\n\n\n"
            + "def main():\n"
            + '    app.run(host="0.0.0.0", debug=True)\n',
        )
    },
    expect_rules=["SEC110", "SEC111"],
)

case(
    "c07_jwt_and_cors",
    "JWT 未校验签名 + CORS 通配凭据",
    {
        "app/api.py": (
            HEAD + "def routes():\n    return []\n",
            HEAD
            + "def routes():\n    return []\n\n\n"
            + "def parse_token(token):\n"
            + '    return jwt.decode(token, options={"verify_signature": False})\n\n\n'
            + "def setup_cors(app):\n"
            + '    allow_origins = ["*"]\n'
            + "    allow_credentials = True\n"
            + "    return allow_origins, allow_credentials\n",
        )
    },
    expect_rules=["SEC112", "SEC113"],
)

case(
    "c08_template_injection",
    "服务端模板注入",
    {
        "app/views.py": (
            HEAD + "def index():\n    return 'ok'\n",
            HEAD
            + "def index():\n    return 'ok'\n\n\n"
            + "def preview(user_tpl, name):\n"
            + "    return render_template_string(user_tpl, name=name)\n",
        )
    },
    expect_rules=["SEC114"],
)

# ==========================================================================
# 性能维度
# ==========================================================================
case(
    "c09_n_plus_one",
    "循环内查库（N+1）+ 循环内拼字符串",
    {
        "app/orders.py": (
            HEAD + "def order_count():\n    return 0\n",
            HEAD
            + "def order_count():\n    return 0\n\n\n"
            + "def load_users(user_ids):\n"
            + "    rows = []\n"
            + "    for uid in user_ids:\n"
            + "        rows.append(repo.query(User).get(uid))\n"
            + "    return rows\n\n\n"
            + "def join_names(rows):\n"
            + '    out = ""\n'
            + "    for row in rows:\n"
            + '        out += str(row) + "\\n"\n'
            + "    return out\n",
        )
    },
    expect_rules=["PERF201", "PERF203"],
)

case(
    "c10_async_block",
    "异步中同步阻塞 + 循环内建连接",
    {
        "app/async_jobs.py": (
            HEAD + "def job_count():\n    return 0\n",
            HEAD
            + "def job_count():\n    return 0\n\n\n"
            + "async def fetch_profile(user_id):\n"
            + '    resp = requests.get("https://api.example.com/profile/%s" % user_id)\n'
            + "    return resp.json()\n\n\n"
            + "def load_all(names):\n"
            + "    conns = []\n"
            + "    for name in names:\n"
            + '        conns.append(sqlite3.connect("app.db"))\n'
            + "    return conns\n",
        )
    },
    expect_rules=["PERF202", "PERF205"],
)

case(
    "c14_unpaged_nested",
    "未分页全量加载 + 嵌套循环线性查找",
    {
        "app/search.py": (
            HEAD + "def limit_default():\n    return 50\n",
            HEAD
            + "def limit_default():\n    return 50\n\n\n"
            + "def load_everything():\n"
            + "    return repo.query(User).all()\n\n\n"
            + "def intersect(items, targets):\n"
            + "    found = []\n"
            + "    for item in items:\n"
            + "        for tag in targets:\n"
            + "            if item.tag == tag:\n"
            + "                found.append(item)\n"
            + "    return found\n",
        )
    },
    expect_rules=["PERF204", "PERF206"],
)

# ==========================================================================
# 规范维度
# ==========================================================================
case(
    "c11_exception",
    "裸 except + 静默吞异常",
    {
        "app/parsing.py": (
            HEAD + "def parse_count(raw):\n    return int(raw)\n",
            HEAD
            + "def parse_count_safe(raw):\n"
            + "    try:\n"
            + "        return int(raw)\n"
            + "    except:\n"
            + "        return 0\n\n\n"
            + "def lookup(mapping, key):\n"
            + "    try:\n"
            + "        return mapping[key]\n"
            + "    except KeyError:\n"
            + "        pass\n",
        )
    },
    expect_rules=["CONV302", "CONV301"],
)

case(
    "c12_defaults_and_files",
    "可变默认参数 + == None + 文件句柄未关闭",
    {
        "app/tags.py": (
            HEAD + "def tag_count():\n    return 0\n",
            HEAD
            + "def tag_count():\n    return 0\n\n\n"
            + "def add_tag(name, tags=[]):\n"
            + "    tags.append(name)\n"
            + "    return tags\n\n\n"
            + "def owner_of(user):\n"
            + "    if user.name == None:\n"
            + "        return 'anonymous'\n"
            + "    return user.name\n\n\n"
            + "def read_config(path):\n"
            + "    fh = open(path)\n"
            + "    return fh.read()\n",
        )
    },
    expect_rules=["CONV305", "CONV306", "CONV308"],
)

case(
    "c13_assert_and_todo",
    "用 assert 做输入校验 + 遗留 TODO",
    {
        "app/validators.py": (
            HEAD + "def ok():\n    return True\n",
            HEAD
            + "def ok():\n    return True\n\n\n"
            + "def withdraw(user_id, amount):\n"
            + '    assert amount > 0, "amount must be positive"\n'
            + "    # TODO: 补上余额校验\n"
            + "    return amount\n",
        )
    },
    expect_rules=["CONV307", "CONV304"],
)

# ==========================================================================
# 语义类（规则库抓不到，只能靠模型）
# ==========================================================================
case(
    "s01_semantics",
    "事务边界缺失 + 超时缺失 + 参数未校验",
    {
        "app/payments.py": (
            HEAD + "def ping():\n    return 'pong'\n",
            HEAD
            + "def ping():\n    return 'pong'\n\n\n"
            + "def process(order_id, amount):\n"
            + "    order = repo.get_order(order_id)\n"
            + "    order.amount = amount\n"
            + "    repo.save(order)\n"
            + "    ledger.insert(order_id, amount)\n"
            + "    requests.post(callback_url, json=order.payload)\n"
            + "    return order\n",
        )
    },
    expect_semantic=[
        "多处写操作之间没有事务边界，中途失败会留下半截数据",
        "外部回调请求没有超时与异常处理",
        "order_id / amount 未做边界校验",
    ],
    note="这些缺陷语法完全合法，正则抓不到；用来衡量模型的语义理解能力。",
)

# ==========================================================================
# 负数对照：干净改动，任何意见都算误报
# ==========================================================================
case(
    "n01_clean_query",
    "干净：参数化查询 + with 管理句柄",
    {
        "app/safe_db.py": (
            HEAD + "def health():\n    return True\n",
            HEAD
            + "def health():\n    return True\n\n\n"
            + "def find_user(conn, username):\n"
            + '    cur = conn.cursor()\n'
            + '    cur.execute("SELECT * FROM users WHERE name = ?", (username,))\n'
            + "    return cur.fetchone()\n\n\n"
            + "def read_text(path):\n"
            + '    with open(path, "r", encoding="utf-8") as fh:\n'
            + "        return fh.read()\n",
        )
    },
    negative=True,
    note="参数化查询 + with open，都不应命中任何规则。",
)

case(
    "n02_clean_http",
    "干净：带超时的外部调用 + 显式异常处理",
    {
        "app/http_client.py": (
            HEAD + "def ready():\n    return True\n",
            HEAD
            + "def ready():\n    return True\n\n\n"
            + "def fetch(url, timeout=5):\n"
            + "    try:\n"
            + "        return requests.get(url, timeout=timeout).json()\n"
            + "    except requests.Timeout:\n"
            + '        logger.warning("upstream timeout: %s", url)\n'
            + "        return None\n",
        )
    },
    negative=True,
    note="显式 timeout + 捕获后记录日志，不应误报「缺少超时处理」。",
)

case(
    "n03_clean_style",
    "干净：类型标注 + is None + 日志",
    {
        "app/service.py": (
            HEAD + "def noop():\n    return None\n",
            HEAD
            + "def noop():\n    return None\n\n\n"
            + "def normalize(name: str | None) -> str:\n"
            + "    if name is None:\n"
            + '        logger.debug("empty name, fallback applied")\n'
            + '        return "anonymous"\n'
            + "    return name.strip().lower()\n",
        )
    },
    negative=True,
    note="is None 判断 + logger 而非 print，不应误报。",
    heldout=False,
)

# ==========================================================================
# 留出集（held-out）：这些用例在调模式库时**没有**参与，专门用来拿一个
# 不那么自欺欺人的数字。自己出题自己答拿 100% 没什么意义。
# ==========================================================================
case(
    "h01_format_sql",
    "[留出] .format() 拼 SQL + 注释里埋反例说明",
    {
        "app/legacy.py": (
            HEAD + "def health():\n    return True\n",
            HEAD
            + "def health():\n    return True\n\n\n"
            + "def query_by_id(conn, user_id):\n"
            + '    cur = conn.cursor()\n'
            + '    cur.execute("SELECT * FROM users WHERE id = {}".format(user_id))\n'
            + "    # 历史遗留：这里以前用 pickle.loads 反序列化过用户数据，已移除\n"
            + "    return cur.fetchone()\n",
        )
    },
    expect_rules=["SEC101"],
    note="注释里提到 pickle.loads 只是说明历史问题，不应触发 SEC105。",
    heldout=True,
)

case(
    "h02_provider_key",
    "[留出] 硬编码第三方 API Key",
    {
        "app/settings.py": (
            HEAD + "TIMEOUT = 30\n",
            HEAD
            + "TIMEOUT = 30\n"
            + 'OPENAI_API_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"\n',
        )
    },
    expect_rules=["SEC103"],
    heldout=True,
)

case(
    "h03_comment_traps",
    "[留出] 全是注释陷阱：注释里写了危险写法，代码本身干净",
    {
        "app/notes.py": (
            HEAD + "def noop():\n    return None\n",
            HEAD
            + "def noop():\n    return None\n\n\n"
            + "def safe_copy(src, dst):\n"
            + "    # 不要用 os.system(cmd) 去拷贝，会有命令注入\n"
            + "    # 也不要写 requests.get(url, verify=False)\n"
            + "    # 更不要图省事用 eval(expr) 解析配置\n"
            + '    with open(src, "rb") as f_in, open(dst, "wb") as f_out:\n'
            + "        f_out.write(f_in.read())\n",
        )
    },
    negative=True,
    note="注释里出现 os.system / verify=False / eval，都不应触发规则。这是注释守卫的关键测试。",
    heldout=True,
)

case(
    "h04_clean_subprocess",
    "[留出] 干净：列表参数调用外部命令 + 带超时的请求",
    {
        "app/tools.py": (
            HEAD + "def ready():\n    return True\n",
            HEAD
            + "def ready():\n    return True\n\n\n"
            + "def list_dir(path):\n"
            + '    return subprocess.run(["ls", "-l", path], capture_output=True, check=False)\n\n\n'
            + "def notify(url, payload):\n"
            + "    try:\n"
            + "        return requests.post(url, json=payload, timeout=3).status_code\n"
            + "    except requests.RequestException:\n"
            + '        logger.warning("notify failed: %s", url)\n'
            + "        return 0\n",
        )
    },
    negative=True,
    note="subprocess 没用 shell=True、requests 带了 timeout 且有异常处理，都不应误报。",
    heldout=True,
)


def main() -> int:
    CORPUS.mkdir(parents=True, exist_ok=True)
    for old in CORPUS.glob("*.patch"):
        old.unlink()

    labels: list[dict] = []
    total_expected = 0
    for c in CASES:
        p = CORPUS / f"{c['id']}.patch"
        p.write_text(c["diff"], encoding="utf-8")
        labels.append(
            {
                "id": c["id"],
                "title": c["title"],
                "negative": c["negative"],
                "heldout": c["heldout"],
                "expect_rules": c["expect_rules"],
                "expect_semantic": c["expect_semantic"],
                "note": c["note"],
                "patch": f"eval/corpus/{c['id']}.patch",
            }
        )
        if not c["negative"]:
            total_expected += len(c["expect_rules"])

    payload = {
        "generated_by": "eval/build_corpus.py",
        "rules_covered": sorted({r for c in CASES for r in c["expect_rules"]}),
        "cases": labels,
        "summary": {
            "cases": len(CASES),
            "negative_cases": sum(1 for c in CASES if c["negative"]),
            "heldout_cases": sum(1 for c in CASES if c["heldout"]),
            "tuned_cases": sum(1 for c in CASES if not c["heldout"]),
            "expected_rule_hits": total_expected,
            "expected_semantic_hits": sum(len(c["expect_semantic"]) for c in CASES),
        },
    }
    LABELS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"生成 {len(CASES)} 个用例 → {CORPUS}")
    print(f"标注文件 → {LABELS}")
    print(f"期望规则命中总数：{total_expected}")
    print(f"覆盖规则：{len(payload['rules_covered'])} 类")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
