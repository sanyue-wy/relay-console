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
from relay.probe import probe_selectors  # noqa: E402


async def main(pid: str, send: str | None, account: str | None):
    cfg = load_config()
    br = ChromeManager(cfg)
    ad = adapters.create(pid, page_provider=br.page,
                         url=cfg["platforms"].get(pid, {}).get("url"), account=account)
    await ad.start()
    input(f"[{pid}:{ad.account}] 如未登录，请在打开的 Chrome 里登录，完成后按回车继续...")
    result = await probe_selectors(ad)
    for f in result["fields"]:
        count = "--" if f["count"] is None else str(f["count"])
        print(f"  {f['field']:13s} {count:>4}  {f['selector']}")
    print(f"  探测结论: {'通过' if result['ok'] else '不通过'}（必检字段：input / last_reply）")
    print("  limit_state:", await ad.limit_state())
    if send:
        await ad.send(Turn("user", send, "user"))
        r = await ad.receive(120)
        print("  reply:", r.text[:300], "| finished:", r.finished)
    chats = await ad.list_chats()
    print("  chats:", len(chats), "个历史会话（前 3：", [c["title"][:12] for c in chats[:3]], "）")
    await br.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", choices=adapters.available())
    ap.add_argument("--send")
    ap.add_argument("--account", help="多账号时指定账号名（对应 config.yaml platforms.<pid>.accounts）")
    a = ap.parse_args()
    asyncio.run(main(a.platform, a.send, a.account))
