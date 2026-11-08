"""A4 开始页界面静态契约：三步弹窗结构齐全、无内联事件（CSP）、动态内容经 esc()。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "relay" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_modal_structure_present():
    for mark in ('id="hoModal"', 'id="hoStep1"', 'id="hoStep2"', 'id="hoStep3"',
                 'data-act="hoRefresh"', 'data-act="hoSaveSummary"',
                 'data-act="hoTo2"', 'data-act="hoTo3"', 'data-act="hoGo"',
                 'id="hoSummary"', 'id="hoPlatform"', 'id="hoAccount"', 'id="hoQuestion"'):
        assert mark in HTML, f"index.html 缺少 {mark}"


def test_actions_registered_in_app_js():
    for act in ("hoOpen", "hoClose", "hoRefresh", "hoSaveSummary", "hoTo2", "hoTo3", "hoGo"):
        assert re.search(rf"\b{act}\b", JS), f"app.js 未实现 {act}"
    # 会话头部的切换按钮由 openS 动态渲染（模板在 app.js），弹窗标题在 HTML
    assert '🔁 总结并切换' in JS and '🔁 总结并切换' in HTML


def test_no_inline_handlers_csp():
    # CSP：不允许内联事件处理器（onclick/onchange/...）
    assert not re.search(r"\son(click|change|load|submit|input|error|mouseover)\s*=", HTML), \
        "index.html 出现内联事件处理器，违反 CSP 约定"
    assert not re.search(r"\son(click|change|load|submit|input|error|mouseover)\s*=", JS)
    # script 只允许外置同源文件
    for m in re.finditer(r"<script[^>]*>", HTML):
        tag = m.group(0)
        assert "src=" in tag, f"发现内联 script：{tag}"


def test_dynamic_content_escaped():
    # 切换提示与预览输出都经过 esc()
    assert "esc(hoFrom)" in JS and "esc(r.platform)" in JS
    assert "$('hoPkg').textContent = r.package" in JS      # 用 textContent 塞原文，天然不解析 HTML
    # 弹窗内 HTML 拼接里的动态值都套了 esc(
    for frag in ("esc(p.id)", "esc(a.account)", "esc(String(r.cooldown_until)"):
        assert frag in JS, f"app.js 缺少转义：{frag}"


def test_step1_to_step2_and_settings_link():
    assert 'data-act="hoTo2"' in HTML and 'data-act="hoGoSettings"' in JS
    # 状态徽标走 B2 状态接口，接口未就绪时降级为「状态未知」
    assert "/status?account=" in JS and "状态未知" in JS
