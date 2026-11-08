"""列出本机指纹浏览器里的环境（profile），用于填 config.yaml 的 fingerprint.profiles。

用法：
    python scripts/fingerprint_profiles.py
    python scripts/fingerprint_profiles.py --backend ovobrowser --api-base http://127.0.0.1:50325

前提：对应客户端已启动，并在设置里开启本地 API（AdsPower 还需在 设置 → API 生成 api-key）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.config import load_config  # noqa: E402
from relay.fingerprint import BACKEND_TITLES, FingerprintError, build_backend  # noqa: E402


async def run(args) -> int:
    cfg = load_config()
    fp_cfg = dict(cfg.get("fingerprint") or {})
    if args.backend:
        fp_cfg["backend"] = args.backend
    if args.api_base:
        fp_cfg["api_base"] = args.api_base
    if args.api_key:
        fp_cfg["api_key"] = args.api_key
    backend_name = str(fp_cfg.get("backend") or "chrome").strip().lower()
    if backend_name == "chrome":
        print("当前 fingerprint.backend=chrome，没有指纹浏览器环境可列。")
        print("先设置 fingerprint.backend 为 adspower 或 ovobrowser（或加 --backend 参数）。")
        return 0

    backend = build_backend(fp_cfg)
    try:
        profiles = await backend.list_profiles()
    finally:
        await backend.aclose()

    title = BACKEND_TITLES.get(backend.name, backend.name)
    if not profiles:
        print(f"{title} 里还没有环境。请先在客户端里创建环境并完成登录，再回来运行本脚本。")
        return 0
    print(f"{title} 环境（{backend.api_base}）：\n")
    print(f"{'环境 ID':<40} {'名称':<24} 分组")
    print("-" * 90)
    for p in profiles:
        print(f"{p.id:<40} {p.name:<24} {p.group}")
    print("\n把上面的环境 ID 填进 config.yaml：")
    print("fingerprint:")
    print(f"  backend: {backend.name}")
    print("  profiles:")
    print(f"    deepseek: {{main: \"{profiles[0].id}\"}}   # 换成对应账号的环境")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="列出指纹浏览器环境 ID")
    parser.add_argument("--backend", choices=["chrome", "adspower", "ovobrowser"])
    parser.add_argument("--api-base", help="本地 API 地址（默认按后端选择）")
    parser.add_argument("--api-key", help="AdsPower api-key（也可用环境变量 ADSPOWER_API_KEY）")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except FingerprintError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
