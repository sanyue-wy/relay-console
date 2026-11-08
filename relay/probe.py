"""选择器探测（B2）：scripts/probe.py 与 POST /api/platforms/{pid}/probe 共用。

只检查 selectors.yaml 各选择器在页面上的匹配数量，不做收发测试。
"""
from __future__ import annotations

# 探测的字段顺序与 scripts/probe.py 原逻辑一致
FIELDS = ("input", "send_button", "stop_button", "last_reply", "new_chat", "upload_input",
          "chat_list_item", "login_user", "login_pass", "login_submit")

# 必须匹配选择器才算通过的字段；其余为可选（未配置或匹配 0 都不判失败）
REQUIRED = ("input", "last_reply")


async def probe_selectors(ad) -> dict:
    """逐个检查 ad.sel 的选择器匹配数。返回 {fields: [...], ok}。

    每行 {field, selector, count, required, pass}；未配置的可选字段 count=None。
    """
    fields = []
    for name in FIELDS:
        css = getattr(ad.sel, name, None)
        if not css:
            fields.append({"field": name, "selector": None, "count": None,
                           "required": name in REQUIRED, "pass": None})
            continue
        n = await ad.page.locator(css).count()
        fields.append({"field": name, "selector": css, "count": n,
                       "required": name in REQUIRED, "pass": n > 0})
    ok = all(f["pass"] for f in fields if f["required"] and f["pass"] is not None)
    return {"fields": fields, "ok": ok}
