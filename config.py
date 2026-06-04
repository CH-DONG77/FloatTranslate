"""Persistent app configuration stored as JSON under %APPDATA%."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


def _config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "FloatTranslate")
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(_config_dir(), "config.json")


@dataclass
class Config:
    # Anthropic API key. Falls back to the ANTHROPIC_API_KEY env var if blank.
    api_key: str = ""
    model: str = "claude-haiku-4-5-20251001"
    target_language: str = "简体中文"
    # OCR source language (BCP-47). Empty = use Windows user-profile languages.
    ocr_language: str = ""
    # Auto mode: re-scan the capture region every N milliseconds.
    auto_interval_ms: int = 1500
    # Window geometry "WxH+X+Y" remembered between runs.
    geometry: str = "520x420+200+200"

    def resolved_api_key(self) -> str:
        return self.api_key.strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
