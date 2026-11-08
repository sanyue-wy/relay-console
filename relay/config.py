from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
    def __getattr__(self, k: str) -> Any:
        v = self[k]
        return Config(v) if isinstance(v, dict) else v


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else ROOT / "config.yaml"
    if not p.exists():
        p = ROOT / "config.example.yaml"
    cfg = Config(yaml.safe_load(p.read_text(encoding="utf-8")))
    for key in ("data_dir", "profiles_dir"):
        d = Path(cfg[key])
        if not d.is_absolute():
            d = ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        cfg[key] = str(d)
    return cfg


def platform_accounts(cfg, pid: str) -> list[str]:
    """某平台配置的账号列表；未配置 accounts 时视为单账号 main。"""
    p = cfg.get("platforms", {}).get(pid) or {}
    accounts = p.get("accounts") or ["main"]
    return list(accounts)


def save_config(cfg) -> None:
    """把当前生效配置整体写回 config.yaml（首次保存会从 example 固化为正式配置）。"""
    p = ROOT / "config.yaml"
    yaml.safe_dump(dict(cfg), p.open("w", encoding="utf-8"),
                   allow_unicode=True, sort_keys=False)
