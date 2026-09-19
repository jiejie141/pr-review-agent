"""缺陷模式库测试。

每条规则都同时钉住**正例**和**反例** —— 只测正例的规则库很容易在「多报」上失控，
而误报恰恰是代码审查工具最致命的失败模式。
"""

import pytest

from pagent.patterns import PATTERNS, catalog, get_pattern, stats_by_category
from pagent.models import Category

# (规则号, 应命中, 不应命中)
CASES: list[tuple[str, str, str]] = [
    ("SEC101", 'cur.execute("SELECT * FROM u WHERE n = \'" + name + "\'")', 'cur.execute("SELECT * FROM u WHERE n = ?", (name,))'),
    ("SEC101", 'conn.execute("DELETE FROM t WHERE id = %s" % uid)', 'conn.execute("DELETE FROM t WHERE id = ?", (uid,))'),
    ("SEC102", "os.system(cmd)", 'subprocess.run(["ls", "-l"], capture_output=True)'),
    ("SEC102", "subprocess.run(cmd, shell=True)", "subprocess.run(cmd_list, check=False)"),
    ("SEC103", 'API_KEY = "abcdefghijklmnop1234"', "api_key = os.environ['API_KEY']"),
    ("SEC103", 'password = "hunter2_super_secret_2024"', "password = get_password_from_env()"),
    ("SEC103", 'token = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"', "token = load_token()"),
    ("SEC104", "result = eval(payload)", "from ast import literal_eval"),
    ("SEC104", "exec(user_code)", "value = ast.literal_eval(raw)"),
    ("SEC105", "obj = pickle.loads(raw)", "obj = json.loads(raw)"),
    ("SEC105", "data = yaml.load(text)", "data = yaml.safe_load(text)"),
    ("SEC106", "requests.get(url, verify=False)", "requests.get(url, verify=True)"),
    ("SEC107", "return hashlib.md5(password.encode()).hexdigest()", "return bcrypt.hashpw(password, salt)"),
    ("SEC108", "requests.get(callback_url)", 'requests.get("https://api.example.com/fixed")'),
    ("SEC109", "open(os.path.join(BASE, filename)).read()", 'open(os.path.join(BASE, "fixed.txt")).read()'),
    ("SEC110", 'app.run(host="0.0.0.0", debug=True)', 'app.run(host="0.0.0.0", debug=settings.debug)'),
    ("SEC111", 'logger.info("login pw=%s", password)', 'logger.info("login user=%s", username)'),
    ("SEC112", 'jwt.decode(token, options={"verify_signature": False})', "jwt.decode(token, algorithms=['RS256'])"),
    ("SEC113", 'allow_origins = ["*"]', 'allow_origins = ["https://app.example.com"]'),
    ("SEC114", "render_template_string(user_tpl)", "render_template('page.html', name=name)"),
    ("PERF201", "for uid in user_ids:\n        rows.append(repo.query(User).get(uid))", "rows = repo.query(User).filter(User.id.in_(user_ids)).all()"),
    ("PERF202", "async def f():\n    time.sleep(1)", "async def f():\n    await asyncio.sleep(1)"),
    ("PERF203", 'for row in rows:\n    out += str(row) + "\\n"', 'parts = [str(r) for r in rows]'),
    ("PERF204", "return repo.query(User).all()", "return repo.query(User).limit(50).all()"),
    ("PERF205", "for name in names:\n        conns.append(sqlite3.connect('app.db'))", "conn = sqlite3.connect('app.db')\nfor name in names:\n    use(conn)"),
    ("PERF206", "for i in items:\n    for t in targets:\n        if i.tag == t:", "tags = {t.name for t in targets}"),
    ("CONV301", "except KeyError:\n        pass", 'except KeyError:\n        logger.warning("missing")'),
    ("CONV302", "    except:", "    except ValueError:"),
    ("CONV303", 'print("debug:", user)', 'logger.debug("user=%s", user)'),
    ("CONV304", "# TODO: 补上余额校验", "# 已完成余额校验"),
    ("CONV305", "def add_tag(name, tags=[]):", "def add_tag(name, tags=None):"),
    ("CONV306", "if user.name == None:", "if user.name is None:"),
    ("CONV307", 'assert amount > 0, "must be positive"', 'raise ValueError("must be positive")'),
    (  # assert + isinstance 也是「拿 assert 当输入校验」，属于正例
        "CONV307",
        "assert isinstance(amount, int)",
        "if not isinstance(amount, int):\n        raise TypeError(\"amount\")",
    ),
    ("CONV308", "fh = open(path)", 'with open(path) as fh:'),
]


@pytest.mark.parametrize("pid,positive,negative", CASES)
def test_pattern_matches_positive(pid, positive, negative):
    p = get_pattern(pid)
    assert p is not None, f"规则 {pid} 不存在"
    assert p.match(positive) is not None, f"{pid} 应命中正例：{positive!r}"


@pytest.mark.parametrize("pid,positive,negative", CASES)
def test_pattern_rejects_negative(pid, positive, negative):
    p = get_pattern(pid)
    assert p.match(negative) is None, f"{pid} 不应命中反例：{negative!r}"


def test_every_pattern_id_is_unique():
    ids = [p.id for p in PATTERNS]
    assert len(ids) == len(set(ids))


def test_rule_count_meets_claim():
    """简历口径是「20+ 类缺陷模式」，这里用测试把它钉住。"""
    assert len(PATTERNS) >= 20


def test_all_three_categories_covered():
    stats = stats_by_category()
    assert stats[Category.SECURITY.value] >= 10
    assert stats[Category.PERFORMANCE.value] >= 5
    assert stats[Category.CONVENTION.value] >= 5


def test_every_case_has_suggestion_and_description():
    for p in PATTERNS:
        assert p.suggestion.strip(), f"{p.id} 缺少修复建议"
        assert p.description.strip(), f"{p.id} 缺少缺陷说明"
        assert 0 < p.confidence <= 1.0, f"{p.id} 置信度越界"


def test_context_dependent_patterns_declared():
    """循环内的问题必须声明 needs_context，否则单行匹配永远抓不到。"""
    for pid in ("PERF201", "PERF202", "PERF203", "PERF205", "PERF206"):
        assert get_pattern(pid).needs_context is True


def test_comment_checker_does_not_skip_comments():
    """CONV304 检查的就是注释内容，必须关掉注释跳过。"""
    assert get_pattern("CONV304").ignore_comments is False
    assert get_pattern("SEC104").ignore_comments is True


def test_extension_filtering():
    assert get_pattern("SEC101").applies_to("a/query.py") is True
    assert get_pattern("SEC101").applies_to("a/style.css") is False
    assert get_pattern("CONV305").applies_to("a/x.js") is False
    assert get_pattern("CONV305").applies_to("a/x.py") is True
    assert get_pattern("SEC102").applies_to("anything.txt") is True  # 未限定扩展名


def test_catalog_output_shape():
    rows = catalog()
    assert len(rows) == len(PATTERNS)
    keys = {"id", "title", "category", "severity", "extensions", "needs_context", "confidence", "description"}
    assert keys.issubset(rows[0].keys())


def test_unknown_pattern_returns_none():
    assert get_pattern("NOPE999") is None
