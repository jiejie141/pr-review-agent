"""配置与自检测试。"""

from pathlib import Path

import pytest

from pagent.config import PROJECT_ROOT, Settings, load_dotenv


# ---------------------------------------------------------------- .env 解析
def test_load_dotenv_parses_simple_pairs(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("FOO_BAR_XYZ=hello\n", encoding="utf-8")
    monkeypatch.delenv("FOO_BAR_XYZ", raising=False)
    assert load_dotenv(p) == 1
    import os

    assert os.environ["FOO_BAR_XYZ"] == "hello"
    monkeypatch.delenv("FOO_BAR_XYZ", raising=False)


def test_load_dotenv_ignores_comments_and_blanks(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment\n\n   \nA_KEY_1=v\n", encoding="utf-8")
    monkeypatch.delenv("A_KEY_1", raising=False)
    assert load_dotenv(p) == 1
    import os

    monkeypatch.delenv("A_KEY_1", raising=False)


def test_load_dotenv_strips_quotes(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('Q_KEY_1="quoted value"\nQ_KEY_2=\'single\'\n', encoding="utf-8")
    import os

    monkeypatch.delenv("Q_KEY_1", raising=False)
    monkeypatch.delenv("Q_KEY_2", raising=False)
    load_dotenv(p)
    assert os.environ["Q_KEY_1"] == "quoted value"
    assert os.environ["Q_KEY_2"] == "single"
    monkeypatch.delenv("Q_KEY_1", raising=False)
    monkeypatch.delenv("Q_KEY_2", raising=False)


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("KEEP_ME_1", "original")
    p = tmp_path / ".env"
    p.write_text("KEEP_ME_1=overwritten\n", encoding="utf-8")
    load_dotenv(p)
    assert os.environ["KEEP_ME_1"] == "original"


def test_load_dotenv_override_flag(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("OVER_ME_1", "original")
    p = tmp_path / ".env"
    p.write_text("OVER_ME_1=new\n", encoding="utf-8")
    load_dotenv(p, override=True)
    assert os.environ["OVER_ME_1"] == "new"


def test_load_dotenv_missing_file_returns_zero(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_load_dotenv_skips_lines_without_equals(tmp_path):
    p = tmp_path / ".env"
    p.write_text("NOEQUALS\nOK_KEY_9=1\n", encoding="utf-8")
    import os

    os.environ.pop("OK_KEY_9", None)
    assert load_dotenv(p) == 1


# ---------------------------------------------------------------- Settings
def test_settings_llm_ready_flag():
    assert Settings(llm_api_key="k" * 30).llm_ready is True
    assert Settings().llm_ready is False


def test_settings_github_ready_flag():
    assert Settings(github_token="t").github_ready is True
    assert Settings().github_ready is False


def test_validate_flags_missing_key_when_needed():
    probs = Settings().validate(need_llm=True)
    assert any("LLM_API_KEY" in p for p in probs)


def test_validate_skips_llm_check_in_mock_mode():
    assert Settings(mock=True).validate(need_llm=True) == []


def test_validate_flags_suspiciously_short_key():
    probs = Settings(llm_api_key="short").validate()
    assert any("长度可疑" in p for p in probs)


def test_validate_flags_bad_base_url():
    probs = Settings(llm_api_key="k" * 30, llm_base_url="ftp://x").validate()
    assert any("URL" in p for p in probs)


def test_validate_flags_tiny_shard_size():
    probs = Settings(llm_api_key="k" * 30, shard_max_chars=100).validate()
    assert any("SHARD_MAX_CHARS" in p for p in probs)


def test_validate_passes_with_sane_values():
    s = Settings(llm_api_key="k" * 40, llm_base_url="https://api.example.com/v1")
    assert s.validate() == []


def test_doctor_report_mentions_all_sections():
    text = Settings().doctor()
    for section in ("环境自检", "[LLM]", "[GitHub]", "[审查策略]", "[自检结论]"):
        assert section in text


def test_doctor_reports_mock_mode():
    assert "离线替身" in Settings(mock=True).doctor()


def test_doctor_reports_missing_key_problem():
    assert "✗" in Settings().doctor()


def test_project_root_is_repo_root():
    assert (PROJECT_ROOT / "main.py").is_file()
    assert (PROJECT_ROOT / "src" / "pagent").is_dir()


# ---------------------------------------------------------------- 规范路径
def test_conventions_falls_back_to_examples():
    conv = Settings().conventions
    assert conv, "应回退到 examples/conventions"
    assert all(Path(c).is_file() for c in conv)


def test_explicit_conventions_paths_win():
    s = Settings(conventions_paths=["/a/b.md", "/c/d.md"])
    assert s.conventions == ["/a/b.md", "/c/d.md"]


def test_resolve_conventions_prefers_repo_docs(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("# 规范", encoding="utf-8")
    s = Settings()
    got = s.resolve_conventions(tmp_path)
    assert len(got) == 1
    assert got[0].endswith("CONTRIBUTING.md")


def test_resolve_conventions_falls_back_when_none_in_repo(tmp_path):
    got = Settings().resolve_conventions(tmp_path)
    assert got == Settings().conventions


# ---------------------------------------------------------------- 字段默认值
def test_defaults_are_conservative():
    s = Settings()
    assert s.dry_run is True, "写操作默认必须是 dry-run"
    assert s.require_line_anchor is True, "行号锚定默认必须开启"
    assert s.mock is False
    assert s.max_findings_per_file > 0
