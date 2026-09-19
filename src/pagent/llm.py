"""LLM 客户端。

三件事必须做扎实，否则线上会很难受：
1. **重试要区分能不能重试** —— 401 重试一百次也是 401，但 429/5xx 值得退避再试；
2. **json_mode 不能强依赖** —— 不是所有 OpenAI 兼容端点都实现 response_format，
   被拒一次就永久降级，不要每个请求都撞一遍墙；
3. **模型输出永远是脏的** —— 会带 markdown 围栏、会带解释性前言、会少引号，
   解析器必须容错，而不是抛异常让整次审查失败。
"""

from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    attempts: int = 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


def _jitter(attempt: int, base: float = 0.8, cap: float = 12.0) -> float:
    """指数退避 + 抖动。抖动是为了避免多个任务同时重试形成脉冲。"""
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() * 0.5)


def parse_json_payload(text: str) -> Any:
    """从模型输出里抠出 JSON。

    依次尝试：直接解析 → 去掉 markdown 围栏 → 截取第一个 [] / {} → 修复尾逗号。
    全失败则抛 LLMError，由上层决定降级策略（而不是静默返回空）。
    """
    if not text or not text.strip():
        raise LLMError("模型返回为空")

    s = text.strip()

    def _try(candidate: str) -> Any | None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    got = _try(s)
    if got is not None:
        return got

    fenced = re.findall(r"```(?:json|JSON)?\s*([\s\S]*?)```", s)
    for block in fenced:
        got = _try(block.strip())
        if got is not None:
            return got

    # 截取最外层的容器。按「起始位置」排序，而不是固定优先数组 ——
    # 否则 `{"findings": [{"line": 3}]}` 会被从内层的 [ 开始截取，
    # 只拿到 findings 数组、丢掉外层对象，行号字段就全没了。
    found: list[tuple[int, str]] = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start = s.find(opener)
        end = s.rfind(closer)
        if start != -1 and end > start:
            found.append((start, s[start : end + 1]))
    found.sort(key=lambda x: x[0])

    for _, candidate in found:
        got = _try(candidate)
        if got is not None:
            return got
        # 尾逗号修复：模型很爱写 {"a":1,}
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
        got = _try(fixed)
        if got is not None:
            return got

    raise LLMError(f"无法从模型输出中解析出 JSON（前 200 字）：{s[:200]}")


def estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，取中间值。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return int(cjk + other / 3.5) + 1


class LLMClient:
    """OpenAI 兼容协议的极简客户端（标准库实现）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: int = 60,
        max_retries: int = 3,
        temperature: float = 0.2,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.supports_json_mode = True
        self.request_count = 0

    # -- 公开接口 ----------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._once(messages, json_mode, temperature, attempt)
            except urllib.error.HTTPError as e:
                body = self._read_error(e)
                # json_mode 不被支持：永久降级后立即重试，不计入退避
                if e.code == 400 and json_mode and self._looks_like_unsupported_format(body):
                    self.supports_json_mode = False
                    continue
                if e.code not in RETRYABLE_STATUS:
                    raise LLMError(f"HTTP {e.code}（不可重试）：{body[:300]}") from e
                last_err = LLMError(f"HTTP {e.code}：{body[:200]}")
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                last_err = LLMError(f"网络异常：{e}")

            if attempt < self.max_retries:
                time.sleep(_jitter(attempt))
        raise last_err or LLMError("调用失败且无异常信息")

    def chat_json(self, messages: list[dict], temperature: float | None = None) -> Any:
        resp = self.chat(messages, json_mode=True, temperature=temperature)
        return parse_json_payload(resp.text)

    # -- 内部 --------------------------------------------------------------
    def _once(
        self,
        messages: list[dict],
        json_mode: bool,
        temperature: float | None,
        attempt: int,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                # 部分网关会拦截默认 UA，显式指定更稳
                "User-Agent": "pr-review-agent/1.0",
            },
        )
        self.request_count += 1
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices：{str(body)[:200]}")
        msg = choices[0].get("message", {})
        usage = body.get("usage") or {}
        return LLMResponse(
            text=msg.get("content") or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=body.get("model", self.model),
            finish_reason=choices[0].get("finish_reason", ""),
            attempts=attempt + 1,
        )

    @staticmethod
    def _read_error(e: urllib.error.HTTPError) -> str:
        try:
            return e.read().decode("utf-8", errors="replace")
        except Exception:
            return str(e)

    @staticmethod
    def _looks_like_unsupported_format(body: str) -> bool:
        low = body.lower()
        return "response_format" in low or "json_object" in low or "unsupported" in low


# ==========================================================================
# 离线替身
# ==========================================================================
_LINE_RE = re.compile(r"^\s*(\d+):\s*([+ -]?)\s?(.*)$")


@dataclass
class MockLLMClient:
    """离线替身。

    它不是「返回固定假数据」—— 它会真的解析输入里带行号的代码，跑一组
    规则覆盖不到的语义启发式，再产出结构化意见。这样断网状态下
    「锚定 → 行号校验 → 去重 → 渲染报告」这整条链路都能被真实地跑到。
    """

    model: str = "mock-reviewer"
    supports_json_mode: bool = True
    request_count: int = 0
    calls: list[list[dict]] = field(default_factory=list)

    def chat(
        self,
        messages: list[dict],
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.request_count += 1
        self.calls.append(messages)
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        findings = self._infer(prompt)
        text = json.dumps({"findings": findings}, ensure_ascii=False)
        return LLMResponse(
            text=text,
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=estimate_tokens(text),
            model=self.model,
            finish_reason="stop",
        )

    def chat_json(self, messages: list[dict], temperature: float | None = None) -> Any:
        return json.loads(self.chat(messages, json_mode=True).text)

    # -- 语义启发式：规则库做不了、需要看上下文的那部分 ---------------------
    def _infer(self, prompt: str) -> list[dict]:
        lines = self._parse_lines(prompt)
        out: list[dict] = []
        # 与规则引擎保持同一套口径：整行注释里的代码是「说明」而不是缺陷。
        # 替身也要守这条，否则离线基线会被注释噪声污染，指标失去参考意义。
        write_ops = sum(
            1
            for _, c in lines
            if re.search(r"\.(?:commit|execute|save|insert|update|delete)\s*\(", c)
        )
        for no, code in lines:
            low = code.lower()
            stripped = code.strip()
            if stripped.startswith(("#", "//", "*", "/*")):
                continue

            # 1) 对外接口直接使用入参，未见校验
            if re.search(r"def\s+\w+\s*\([^)]*\b(?:user_id|order_id|filename|amount|count)\b", code):
                if not self._has_nearby(prompt, no, r"(?:is None|<=\s*0|len\s*\(|raise\s+ValueError|assert)"):
                    out.append(
                        self._mk(
                            no,
                            "参数未做边界校验就直接使用",
                            "convention",
                            "medium",
                            stripped,
                            "在函数入口校验参数范围与类型，非法输入尽早 raise，不要留给下游。",
                            0.6,
                        )
                    )

            # 2) 多个写操作缺少事务边界
            if write_ops >= 2 and re.search(
                r"\.(?:commit|execute|save|insert|update|delete)\s*\(", low
            ):
                if not self._has_nearby(
                    prompt, no, r"(?:transaction|with\s+conn|BEGIN|SessionLocal|begin\s*\(|atomic)"
                ):
                    out.append(
                        self._mk(
                            no,
                            "多处写操作未见事务边界",
                            "convention",
                            "medium",
                            stripped,
                            "把相关的写操作包进同一个事务，任一步失败整体回滚，避免留下半截数据。",
                            0.55,
                        )
                    )

            # 3) 外部调用没有错误处理
            if re.search(r"(?:requests\.(?:get|post)|httpx\.|urlopen|fetch\s*\(|axios\.)", low):
                if not self._has_nearby(prompt, no, r"(?:try|except|catch|timeout\s*=)"):
                    out.append(
                        self._mk(
                            no,
                            "外部请求缺少超时与异常处理",
                            "performance",
                            "medium",
                            stripped,
                            "显式设置 timeout，并捕获异常后走降级分支；外部依赖不可用不应拖垮整个请求。",
                            0.6,
                        )
                    )

            # 4) 除法 / 取余未防零
            if re.search(r"[^/\s]/[^/=]", code) and re.search(r"\b(?:count|total|len|size|n)\b", low):
                if not self._has_nearby(prompt, no, r"(?:if\s+\w+\s*(?:==|!=|>|<)|or\s+1\b)"):
                    out.append(
                        self._mk(
                            no,
                            "除法运算未处理分母为零",
                            "convention",
                            "low",
                            stripped,
                            "运算前判断分母，为零时给默认值或提前返回。",
                            0.5,
                        )
                    )

            # 5) 缓存 / 全局状态被就地修改
            if re.search(r"(?:global|cache|_CACHE|REGISTRY)\s*\[[^\]]+\]\s*=|(?:global\s+\w+)\s*$", code, re.I):
                out.append(
                    self._mk(
                        no,
                        "就地修改全局可变状态",
                        "convention",
                        "medium",
                        stripped,
                        "全局状态在并发下会产生竞态；改成显式传参或加锁保护。",
                        0.55,
                    )
                )

        return out

    @staticmethod
    def _has_nearby(prompt: str, no: int, pat: str) -> bool:
        """在当前行上下 5 行范围内找是否存在该模式。"""
        lines = prompt.split("\n")
        idx = next((i for i, l in enumerate(lines) if l.strip().startswith(f"{no}:")), None)
        if idx is None:
            return False
        window = "\n".join(lines[max(0, idx - 5) : idx + 6])
        return re.search(pat, window, re.I) is not None

    @staticmethod
    def _parse_lines(prompt: str) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for line in prompt.split("\n"):
            m = _LINE_RE.match(line)
            if not m:
                continue
            out.append((int(m.group(1)), m.group(3)))
        return out

    @staticmethod
    def _mk(no: int, title: str, cat: str, sev: str, evidence: str, suggestion: str, conf: float) -> dict:
        return {
            "line": no,
            "title": title,
            "category": cat,
            "severity": sev,
            "evidence": evidence[:200],
            "suggestion": suggestion,
            "confidence": conf,
        }


def build_client(settings) -> Any:
    if settings.mock:
        return MockLLMClient()
    return LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
    )
