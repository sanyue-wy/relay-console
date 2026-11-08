"""通用网页适配器的回复起始判断（虚拟列表兼容）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.web_generic import GenericWebAdapter  # noqa: E402


def _adapter():
    ad = object.__new__(GenericWebAdapter)
    ad._baseline = 5
    ad._baseline_text = "old reply"
    ad._last_sent = "my question"
    return ad


def test_new_reply_by_count():
    assert _adapter()._is_started(6, "old reply") is True


def test_virtual_list_detects_changed_text():
    # Hark 这类虚拟列表：消息条数不变，旧消息被回收，只能靠最后一条文本变化判断
    assert _adapter()._is_started(5, "new reply") is True


def test_ignores_same_text_and_own_question():
    ad = _adapter()
    assert ad._is_started(5, "old reply") is False
    assert ad._is_started(5, "my question") is False
    assert ad._is_started(5, "") is False
