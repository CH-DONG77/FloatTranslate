"""Translation via a pluggable LLM provider, with a small in-memory cache."""
from __future__ import annotations

from providers import get_provider

_SYSTEM = (
    "You are a translation engine embedded in a screen-translation tool. "
    "The input text comes from OCR of a screenshot, so it may contain minor "
    "recognition errors, broken words, or stray characters — silently correct "
    "obvious ones. Translate the text into {target}. "
    "Output ONLY the translation, with no quotes, no explanations, no "
    "transliteration, and no notes. Preserve line breaks where meaningful. "
    "If the text is already in the target language, return it unchanged."
)


class Translator:
    def __init__(self, provider_id: str, api_key: str, model: str,
                 target_language: str):
        if not api_key:
            raise ValueError("missing_api_key")
        self._provider = get_provider(provider_id)
        self._api_key = api_key
        self._model = model
        self._target = target_language
        self._cache: dict[str, str] = {}

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if text in self._cache:
            return self._cache[text]

        system = _SYSTEM.format(target=self._target)
        out = self._provider.translate(self._api_key, self._model, system, text)

        # Bound cache growth.
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[text] = out
        return out
