"""Persistent app configuration stored as JSON under %APPDATA%."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

import providers


def _config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "FloatTranslate")
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(_config_dir(), "config.json")


@dataclass
class Config:
    # Selected LLM provider id (see providers.py): anthropic/openai/deepseek/google.
    provider: str = "anthropic"
    # Per-provider API keys: {provider_id: key}. Each provider also falls back
    # to its environment variable (see providers.ENV_VARS) when blank.
    api_keys: dict = field(default_factory=dict)
    model: str = "claude-haiku-4-5-20251001"
    target_language: str = "简体中文"
    # OCR source language (BCP-47). Empty = use Windows user-profile languages.
    ocr_language: str = ""
    # Auto mode: re-scan the capture region every N milliseconds.
    auto_interval_ms: int = 1500
    # Window geometry "WxH+X+Y" remembered between runs.
    geometry: str = "520x420+200+200"
    # Legacy single Anthropic key (pre-multi-provider); migrated into api_keys.
    api_key: str = ""

    def resolved_api_key(self, provider: str | None = None) -> str:
        """The usable key for `provider` (or the current one): stored, else env."""
        provider = provider or self.provider
        key = (self.api_keys.get(provider) or "").strip()
        if key:
            return key
        for env in providers.ENV_VARS.get(provider, []):
            val = os.environ.get(env, "").strip()
            if val:
                return val
        return ""

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            cfg = cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            cfg = cls()
        # Migrate the old single key into the per-provider store.
        if cfg.api_key and not cfg.api_keys.get("anthropic"):
            cfg.api_keys["anthropic"] = cfg.api_key
            cfg.api_key = ""
        return cfg

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
