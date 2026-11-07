"""五个网页平台。新增平台：在 selectors.yaml 加一段，再仿照下面写一个类并 @register。"""
from __future__ import annotations

from pathlib import Path

import yaml

from .spec import AdapterInfo, Cap, Kind, SelectorSet, register
from .web_generic import GenericWebAdapter

_SEL_FILE = Path(__file__).with_name("selectors.yaml")
WEB_CAPS = Cap.NEW_CHAT | Cap.LIMIT_DETECT | Cap.RESUME_URL | Cap.FILE_UPLOAD


def load_selectors(pid: str, url_override: str | None = None) -> SelectorSet:
    raw = yaml.safe_load(_SEL_FILE.read_text(encoding="utf-8"))[pid]
    if url_override:
        raw["url"] = url_override
    raw["limit_texts"] = tuple(raw.get("limit_texts") or ())
    raw["limit_status_codes"] = tuple(raw.get("limit_status_codes") or (429,))
    return SelectorSet(**raw)


class _Base(GenericWebAdapter):
    ID = ""
    NAME = ""
    MAX_INPUT = 30000

    def __init__(self, page_provider, url: str | None = None):
        info = AdapterInfo(self.ID, self.NAME, Kind.WEB, WEB_CAPS, max_input_chars=self.MAX_INPUT)
        super().__init__(info, load_selectors(self.ID, url), page_provider)


@register("deepseek")
class DeepSeekAdapter(_Base):
    ID, NAME, MAX_INPUT = "deepseek", "DeepSeek", 30000


@register("doubao")
class DoubaoAdapter(_Base):
    ID, NAME, MAX_INPUT = "doubao", "豆包", 20000


@register("kimi")
class KimiAdapter(_Base):
    ID, NAME, MAX_INPUT = "kimi", "Kimi", 50000


@register("gemini")
class GeminiAdapter(_Base):
    ID, NAME, MAX_INPUT = "gemini", "Gemini", 30000


@register("hark")
class HarkAdapter(_Base):
    ID, NAME, MAX_INPUT = "hark", "Hark", 20000
