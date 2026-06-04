"""LLM providers: validate an API key, list models, and translate.

Each provider talks to its own REST API directly via httpx, so no per-vendor
SDK is needed. `list_models()` doubles as key validation — it performs an
authenticated request and raises `ProviderError` on an auth/network failure,
otherwise returns the models that can be used for translation.
"""
from __future__ import annotations

import httpx

_TIMEOUT = httpx.Timeout(30.0)


class ProviderError(RuntimeError):
    """A user-facing failure (bad key, network error, API error)."""


# --------------------------------------------------------------------------- #
#  Base
# --------------------------------------------------------------------------- #
class Provider:
    id: str = ""
    label: str = ""

    def list_models(self, api_key: str) -> list[str]:
        raise NotImplementedError

    def translate(self, api_key: str, model: str, system: str, text: str) -> str:
        raise NotImplementedError

    # ---- shared HTTP helpers --------------------------------------------- #
    def _request(self, method: str, url: str, *, headers=None, json_body=None) -> dict:
        try:
            r = httpx.request(method, url, headers=headers, json=json_body,
                              timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ProviderError(f"网络错误：{exc}") from exc
        if r.status_code in (401, 403):
            raise ProviderError(f"API Key 无效或无权限（{r.status_code}）")
        if r.status_code >= 400:
            raise ProviderError(f"请求失败 {r.status_code}：{self._err_msg(r)}")
        try:
            return r.json()
        except Exception as exc:  # noqa: BLE001 — surface a readable message
            raise ProviderError(f"响应解析失败：{exc}") from exc

    @staticmethod
    def _err_msg(r: httpx.Response) -> str:
        try:
            j = r.json()
            if isinstance(j, dict):
                err = j.get("error")
                if isinstance(err, dict):
                    return err.get("message") or str(err)
                if isinstance(err, str):
                    return err
                if "message" in j:
                    return str(j["message"])
        except Exception:
            pass
        return (r.text or "").strip()[:200]


# --------------------------------------------------------------------------- #
#  Anthropic (Claude)
# --------------------------------------------------------------------------- #
class AnthropicProvider(Provider):
    id = "anthropic"
    label = "Anthropic (Claude)"
    _VERSION = "2023-06-01"

    def _headers(self, api_key: str) -> dict:
        return {"x-api-key": api_key, "anthropic-version": self._VERSION}

    def list_models(self, api_key: str) -> list[str]:
        data = self._request("GET", "https://api.anthropic.com/v1/models?limit=1000",
                             headers=self._headers(api_key))
        return [m["id"] for m in data.get("data", []) if m.get("id")]

    def translate(self, api_key: str, model: str, system: str, text: str) -> str:
        body = {
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": text}],
        }
        data = self._request("POST", "https://api.anthropic.com/v1/messages",
                             headers=self._headers(api_key), json_body=body)
        return "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        ).strip()


# --------------------------------------------------------------------------- #
#  OpenAI-compatible (OpenAI, DeepSeek)
# --------------------------------------------------------------------------- #
class OpenAICompatProvider(Provider):
    """Any vendor exposing the OpenAI /v1 chat + models endpoints."""

    def __init__(self, id: str, label: str, base_url: str, chat_filter: bool = False):
        self.id = id
        self.label = label
        self._base = base_url.rstrip("/")
        self._chat_filter = chat_filter

    def _headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}

    def list_models(self, api_key: str) -> list[str]:
        data = self._request("GET", f"{self._base}/models",
                             headers=self._headers(api_key))
        ids = [m["id"] for m in data.get("data", []) if m.get("id")]
        if self._chat_filter:
            ids = self._only_chat(ids) or ids
        return sorted(ids)

    @staticmethod
    def _only_chat(ids: list[str]) -> list[str]:
        keep = ("gpt-", "o1", "o3", "o4", "chatgpt")
        drop = ("audio", "realtime", "transcribe", "tts", "image", "embedding",
                "moderation", "search", "dall-e", "whisper", "-instruct")
        return [i for i in ids
                if i.startswith(keep) and not any(d in i for d in drop)]

    def translate(self, api_key: str, model: str, system: str, text: str) -> str:
        body = {
            "model": model,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        }
        data = self._request("POST", f"{self._base}/chat/completions",
                             headers=self._headers(api_key), json_body=body)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("无返回结果")
        return (choices[0].get("message", {}).get("content") or "").strip()


# --------------------------------------------------------------------------- #
#  Google (Gemini)
# --------------------------------------------------------------------------- #
class GoogleProvider(Provider):
    id = "google"
    label = "Google (Gemini)"
    _BASE = "https://generativelanguage.googleapis.com/v1beta"

    def list_models(self, api_key: str) -> list[str]:
        data = self._request("GET", f"{self._BASE}/models?key={api_key}&pageSize=1000")
        out = []
        for m in data.get("models", []):
            name = m.get("name", "")  # "models/gemini-1.5-flash"
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods and name.startswith("models/gemini"):
                out.append(name.split("/", 1)[1])
        return sorted(set(out))

    def translate(self, api_key: str, model: str, system: str, text: str) -> str:
        url = f"{self._BASE}/models/{model}:generateContent?key={api_key}"
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"maxOutputTokens": 1024},
        }
        data = self._request("POST", url, json_body=body)
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError("无返回结果（可能触发了安全拦截）")
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()


# --------------------------------------------------------------------------- #
#  Registry
# --------------------------------------------------------------------------- #
_PROVIDERS: dict[str, Provider] = {
    p.id: p for p in (
        AnthropicProvider(),
        OpenAICompatProvider("openai", "OpenAI", "https://api.openai.com/v1",
                             chat_filter=True),
        OpenAICompatProvider("deepseek", "DeepSeek", "https://api.deepseek.com/v1"),
        GoogleProvider(),
    )
}

# Shown in the model dropdown before validation fetches the live list.
_DEFAULT_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6",
                  "claude-opus-4-8"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "google": ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
}

# Environment variables consulted (in order) when no key is stored for a vendor.
ENV_VARS: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
}


def provider_ids() -> list[str]:
    return list(_PROVIDERS)


def get_provider(provider_id: str) -> Provider:
    try:
        return _PROVIDERS[provider_id]
    except KeyError:
        raise ProviderError(f"未知服务商：{provider_id}") from None


def provider_label(provider_id: str) -> str:
    p = _PROVIDERS.get(provider_id)
    return p.label if p else provider_id


def default_models(provider_id: str) -> list[str]:
    return list(_DEFAULT_MODELS.get(provider_id, []))
