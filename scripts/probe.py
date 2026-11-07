"""探测某个平台的选择器是否有效：python scripts/probe.py deepseek

会打开该平台的 Chrome（首次请手动登录），逐个检查 selectors.yaml 里的选择器匹配数量。
可加 --send "你好" 做一次完整收发测试。
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapters  # noqa: E402
from adapters.spec import Turn  # noqa: E402
from relay.browser import ChromeManager  # noqa: E402
from relay.config import load_config  # noqa: E402


async def main(pid: str, send: str | None):
    cfg = load_config()
    br = ChromeManager(cfg)
    ad = adapters.create(pid, page_provider=br.page, url=cfg["platforms"].get(pid, {}).get("url"))
    await ad.start()
    input(f"[{pid}] 如未登录，请在打开的 Chrome 里登录，完成后按回车继续...")
    sel = ad.sel
    for field in ("input", "send_button", "stop_button", "last_reply", "new_chat", "upload_input"):
        css = getattr(sel, field)
        n = await ad.page.locator(css).count() if css else "-"
        print(f"  {field:13s} {str(n):>4}  {css}")
    print("  limit_state:", await ad.limit_state())
    if send:
        await ad.send(Turn("user", send, "user"))
        r = await ad.receive(120)
        print("  reply:", r.text[:300], "| finished:", r.finished)
    await br.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", choices=adapters.available())
    ap.add_argument("--send")
    a = ap.parse_args()
    asyncio.run(main(a.platform, a.send))
