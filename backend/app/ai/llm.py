"""Accesso all'Anthropic Claude API.

- La chiave è letta lato server (ANTHROPIC_API_KEY nel .env) e non viene mai esposta al frontend.
- Output strutturati: lo schema JSON è imposto dall'API (output_config.format) e poi rivalidato qui.
- Tool use: ciclo manuale con strumenti tipizzati (strict) eseguiti dal backend, in sola lettura.
- Ogni chiamata restituisce i metadati necessari al log (modello effettivo, request id, token, latenza).
- Se la chiave manca, `get_llm()` restituisce None e i servizi passano alla modalità deterministica.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import get_settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """Errore della chiamata al modello (rete, limiti, rifiuto, output non valido)."""

    def __init__(self, message: str, *, status: str = "ERROR", meta: dict | None = None):
        super().__init__(message)
        self.status = status
        self.meta = meta or {}


@dataclass
class LLMResult:
    data: Any
    meta: dict = field(default_factory=dict)


ToolHandler = Callable[[str, dict], Any]


class LLM(Protocol):
    model: str

    def structured(self, *, system: str, user: str, schema: dict, max_tokens: int | None = None) -> LLMResult: ...

    def run_tools(self, *, system: str, messages: list[dict], tools: list[dict], handler: ToolHandler,
                  max_steps: int) -> LLMResult: ...


def _meta(resp, started: float) -> dict:
    u = getattr(resp, "usage", None)
    return {
        "model": getattr(resp, "model", None),
        "request_id": getattr(resp, "_request_id", None),
        "stop_reason": getattr(resp, "stop_reason", None),
        "usage": {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
                  "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None)} if u else {},
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _merge_usage(total: dict, part: dict) -> dict:
    for k, v in (part or {}).items():
        if isinstance(v, int):
            total[k] = (total.get(k) or 0) + v
    return total


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, effort: str, max_tokens: int, timeout: float, fallback: bool):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model, self.effort, self.max_tokens, self.fallback = model, effort, max_tokens, fallback

    def _create(self, **kw):
        a = self._anthropic
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if self.fallback else {}
        try:
            return self.client.beta.messages.create(model=self.model, **extra, **kw)
        except a.AuthenticationError as e:
            raise LLMError("Chiave Anthropic non valida o revocata") from e
        except a.PermissionDeniedError as e:
            raise LLMError("La chiave Anthropic non ha accesso a questo modello") from e
        except a.RateLimitError as e:
            raise LLMError("Limite di richieste Anthropic raggiunto: riprovare più tardi") from e
        except a.BadRequestError as e:
            raise LLMError(f"Richiesta rifiutata dall'API: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"Errore dell'API Anthropic ({e.status_code})") from e
        except a.APIConnectionError as e:
            raise LLMError("API Anthropic non raggiungibile") from e

    def structured(self, *, system: str, user: str, schema: dict, max_tokens: int | None = None) -> LLMResult:
        started = time.monotonic()
        resp = self._create(
            max_tokens=max_tokens or self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        meta = _meta(resp, started)
        if resp.stop_reason == "refusal":
            raise LLMError("Il modello ha rifiutato la richiesta", status="REFUSED", meta=meta)
        if resp.stop_reason == "max_tokens":
            raise LLMError("Risposta troncata (max_tokens)", meta=meta)
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise LLMError("Nessun output testuale", meta=meta)
        try:
            return LLMResult(json.loads(text), meta)
        except json.JSONDecodeError as e:
            raise LLMError("Output JSON non valido", meta=meta) from e

    def run_tools(self, *, system: str, messages: list[dict], tools: list[dict], handler: ToolHandler,
                  max_steps: int) -> LLMResult:
        started = time.monotonic()
        msgs = list(messages)
        usage: dict = {}
        calls: list[dict] = []
        resp = None
        for _ in range(max_steps):
            resp = self._create(max_tokens=self.max_tokens, system=system, messages=msgs, tools=tools,
                                tool_choice={"type": "auto"}, output_config={"effort": self.effort})
            _merge_usage(usage, _meta(resp, started)["usage"])
            if resp.stop_reason == "refusal":
                raise LLMError("Il modello ha rifiutato la richiesta", status="REFUSED",
                               meta=_meta(resp, started) | {"usage": usage, "tool_calls": calls})
            uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                break
            msgs.append({"role": "assistant", "content": resp.content})
            results = []
            for b in uses:
                try:
                    out = handler(b.name, dict(b.input or {}))
                    calls.append({"tool": b.name, "input": dict(b.input or {}), "output": out})
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, ensure_ascii=False, default=str)})
                except Exception as e:  # noqa: BLE001 - l'errore torna al modello come tool_result
                    calls.append({"tool": b.name, "input": dict(b.input or {}), "error": str(e)})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": f"Errore: {e}"})
            msgs.append({"role": "user", "content": results})
        else:
            raise LLMError("Troppi passaggi di strumenti senza risposta finale",
                           meta=_meta(resp, started) | {"usage": usage, "tool_calls": calls})
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return LLMResult(text, _meta(resp, started) | {"usage": usage, "tool_calls": calls})


_override: LLM | None = None
_use_override = False


def set_llm_override(llm: LLM | None, active: bool = True) -> None:
    """Per i test: inietta un modello finto (o None per forzare la modalità deterministica)."""
    global _override, _use_override
    _override, _use_override = llm, active


def get_llm() -> LLM | None:
    if _use_override:
        return _override
    s = get_settings()
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value().strip():
        return None
    return AnthropicLLM(s.anthropic_api_key.get_secret_value().strip(), s.ai_model, s.ai_effort, s.ai_max_tokens,
                        s.ai_timeout_seconds, s.ai_refusal_fallback)


def ai_status() -> dict:
    s = get_settings()
    llm = get_llm()
    return {"enabled": llm is not None, "model": getattr(llm, "model", None) if llm else None,
            "configured_model": s.ai_model, "effort": s.ai_effort,
            "mode": "ai" if llm is not None else "deterministic"}
