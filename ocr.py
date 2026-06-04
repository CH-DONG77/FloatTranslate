"""Windows built-in OCR wrapped for synchronous use from worker threads.

Uses the Windows.Media.Ocr WinRT API via the PyWinRT (`winrt-*`) packages.
No external OCR engine or model download is required: recognition runs on the
language packs already installed in Windows (Settings > Time & Language).
"""
from __future__ import annotations

import asyncio
import io

from PIL import Image

from winrt.windows.media.ocr import OcrEngine
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap
from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter


def available_languages() -> list[str]:
    """BCP-47 tags Windows can OCR, e.g. ['en-US', 'zh-Hans-CN']."""
    return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]


def _make_engine(lang_tag: str | None) -> OcrEngine | None:
    """Create an OCR engine for a specific language, or the user's profile."""
    if lang_tag:
        engine = OcrEngine.try_create_from_language(Language(lang_tag))
        if engine is not None:
            return engine
    return OcrEngine.try_create_from_user_profile_languages()


async def _recognize(img: Image.Image, lang_tag: str | None) -> str:
    # PIL image -> PNG bytes -> WinRT stream -> SoftwareBitmap.
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap: SoftwareBitmap = await decoder.get_software_bitmap_async()

    engine = _make_engine(lang_tag)
    if engine is None:
        raise RuntimeError(
            "No OCR engine available. Install a language pack in "
            "Windows Settings > Time & Language > Language."
        )

    result = await engine.recognize_async(bitmap)
    # Join lines ourselves so layout is preserved better than result.text.
    return "\n".join(line.text for line in result.lines)


def recognize(img: Image.Image, lang_tag: str | None = None) -> str:
    """Synchronously OCR a PIL image. Safe to call from a worker thread.

    `lang_tag` is a BCP-47 code (e.g. 'en-US', 'ja'). None = user profile.
    """
    return asyncio.run(_recognize(img, lang_tag))


if __name__ == "__main__":
    print("Available OCR languages:", available_languages())
