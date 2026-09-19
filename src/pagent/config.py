"""配置与启动自检。

刻意不引入 python-dotenv：整个项目运行时零第三方依赖，clone 下来直接能跑。
自己解析 .env 只要 20 行，不值得为此增加一个依赖和一个版本冲突风险。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONVENTIONS = PROJECT_ROOT / "examples" / "conventions"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def load_dotenv(path: Path | str = DEFAULT_ENV_FILE, override: bool = False) -> int:
    """极简 .env 加载：只支持 KEY=VALUE、# 注释、可选引号。已存在的环境变量不覆盖。"""
    p = Path(path)
    if not p.is_file():
        return 0
    loaded = 0
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # --- LLM ---
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60
    llm_max_retries: int = 3
    llm_temperature: float = 0.2

    # --- GitHub ---
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    # MCP GitHub Server 的启动命令（可选）。配了就优先走 MCP，否则退回直连 REST。
    mcp_server_cmd: str = ""

    # --- 审查策略 ---
    conventions_paths: list[str] = field(default_factory=list)
    shard_max_chars: int = 6000
    shard_max_files: int = 8
    max_findings_per_file: int = 6
    max_files: int = 60
    min_severity: str = "low"
    # 无行号的意见一律降级为「风险提示」，不作为行内评论发出
    require_line_anchor: bool = True

    # --- 检索后端 ---
    # bm25   ：纯标准库 BM25（默认，零依赖、可复现）
    # vector ：Chroma 向量检索
    # hybrid ：BM25 + 向量，RRF 融合
    retrieval_backend: str = "bm25"

    # --- 运行时（由 CLI 覆盖）---
    mock: bool = False
    dry_run: bool = True
    offline: bool = False

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def github_ready(self) -> bool:
        return bool(self.github_token)

    @property
    def conventions(self) -> list[str]:
        if self.conventions_paths:
            return self.conventions_paths
        if DEFAULT_CONVENTIONS.is_dir():
            return [str(p) for p in sorted(DEFAULT_CONVENTIONS.glob("*.md"))]
        return []

    def resolve_conventions(self, repo_root: str | Path | None = None) -> list[str]:
        """优先用 PR 仓库自己的规范文档，找不到再退回内置示例。"""
        if self.conventions_paths:
            return self.conventions_paths
        if repo_root:
            root = Path(repo_root)
            for pat in ("CONTRIBUTING.md", "CODE_STYLE.md", "docs/CONVENTIONS.md", ".editorconfig"):
                p = root / pat
                if p.is_file():
                    return [str(p)]
        return self.conventions

    def validate(self, need_llm: bool = True) -> list[str]:
        """返回问题列表，空表示自检通过。"""
        problems: list[str] = []
        if need_llm and not self.mock and not self.llm_api_key:
            problems.append(
                "缺少 LLM_API_KEY：要么在 .env 里配置，要么加 --mock 用离线替身跑通流程。"
            )
        if self.llm_api_key and len(self.llm_api_key) < 20:
            problems.append(f"LLM_API_KEY 长度可疑（{len(self.llm_api_key)} 字符），大概率填错了。")
        if not self.llm_base_url.startswith(("http://", "https://")):
            problems.append(f"LLM_BASE_URL 不是合法 URL：{self.llm_base_url}")
        if self.shard_max_chars < 500:
            problems.append("SHARD_MAX_CHARS 太小（<500），会把一个文件切得七零八落。")
        return problems

    def doctor(self) -> str:
        """人类可读的自检报告。"""
        lines = ["=== pr-review-agent 环境自检 ===", ""]
        lines.append(f"项目根目录      : {PROJECT_ROOT}")
        lines.append(f"Python          : {os.sys.version.split()[0]}")
        lines.append(f"git 可用        : {'是' if shutil.which('git') else '否'}")
        lines.append("")
        lines.append("[LLM]")
        lines.append(f"  模式          : {'离线替身 (--mock)' if self.mock else '真实调用'}")
        lines.append(f"  已配置 Key    : {'是' if self.llm_ready else '否'}")
        lines.append(f"  Base URL      : {self.llm_base_url}")
        lines.append(f"  模型          : {self.llm_model}")
        lines.append(f"  超时/重试     : {self.llm_timeout}s / {self.llm_max_retries} 次")
        lines.append("")
        lines.append("[GitHub]")
        lines.append(f"  Token         : {'已配置' if self.github_ready else '未配置（只能读本地 diff）'}")
        lines.append(f"  API Base      : {self.github_api_base}")
        lines.append(f"  MCP 传输      : {self.mcp_server_cmd or '未配置（走直连 REST）'}")
        lines.append("")
        lines.append("[审查策略]")
        lines.append(f"  分片大小上限  : {self.shard_max_chars} 字符")
        lines.append(f"  单文件上限    : {self.max_findings_per_file} 条")
        lines.append(f"  文件数上限    : {self.max_files}")
        lines.append(f"  最低严重级别  : {self.min_severity}")
        lines.append(f"  强制行号锚定  : {'是' if self.require_line_anchor else '否'}")
        conv = self.conventions
        lines.append(f"  规范文档      : {len(conv)} 个")
        for c in conv[:5]:
            lines.append(f"    - {c}")
        lines.append(f"  检索后端      : {self.retrieval_backend}")
        if self.retrieval_backend in ("vector", "hybrid"):
            try:
                import chromadb  # noqa: F401

                lines.append("    Chroma      : 已安装")
            except ImportError:
                lines.append(
                    "    Chroma      : ✗ 未安装（pip install chromadb），"
                    "运行时会自动退回纯 BM25"
                )
        lines.append("")
        probs = self.validate(need_llm=not self.mock)
        lines.append("[自检结论]")
        if probs:
            for p in probs:
                lines.append(f"  ✗ {p}")
        else:
            lines.append("  ✓ 未发现问题，可以开始审查。")
        return "\n".join(lines)


_SETTINGS: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _SETTINGS
    if _SETTINGS is not None and not reload:
        return _SETTINGS

    load_dotenv()
    conv_raw = os.environ.get("CONVENTIONS_PATHS", "").strip()
    conv = [c.strip() for c in conv_raw.split(os.pathsep) if c.strip()] if conv_raw else []

    _SETTINGS = Settings(
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/"),
        llm_model=os.environ.get("LLM_MODEL", "deepseek-chat").strip(),
        llm_timeout=_env_int("LLM_TIMEOUT", 60),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 3),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.2),
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        github_api_base=os.environ.get("GITHUB_API_BASE", "https://api.github.com").strip().rstrip("/"),
        mcp_server_cmd=os.environ.get("MCP_GITHUB_SERVER_CMD", "").strip(),
        conventions_paths=conv,
        shard_max_chars=_env_int("SHARD_MAX_CHARS", 6000),
        shard_max_files=_env_int("SHARD_MAX_FILES", 8),
        max_findings_per_file=_env_int("MAX_FINDINGS_PER_FILE", 6),
        max_files=_env_int("MAX_FILES", 60),
        min_severity=os.environ.get("MIN_SEVERITY", "low").strip().lower(),
        require_line_anchor=_env_bool("REQUIRE_LINE_ANCHOR", True),
        retrieval_backend=os.environ.get("RETRIEVAL_BACKEND", "bm25").strip().lower(),
        mock=_env_bool("MOCK", False),
        dry_run=_env_bool("DRY_RUN", True),
        offline=_env_bool("OFFLINE", False),
    )
    return _SETTINGS
