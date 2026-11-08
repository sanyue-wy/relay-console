"""配置加载与数据目录。

程序与数据分离：
  - 源码运行：数据默认仍在仓库根（config.yaml / data/ / profiles/），与历史版本一致；
  - exe 运行（PyInstaller 冻结）：数据统一放在 %LOCALAPPDATA%/RelayConsole，
    程序目录可整体替换，升级直接继承旧数据；
  - 两种方式都可用环境变量 RELAY_HOME 显式指定数据根目录（优先级最高）。

升级继承的三层保障：数据库缺列由 _migrate_* 自动补齐；密钥存 Windows 凭据管理器、
不随程序目录走；老 config.yaml 缺少的新键由 _deep_fill 从 config.example.yaml 补齐
（只补缺、不覆盖已有值）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
APP_VERSION = "1.1.0"


class Config(dict):
    def __getattr__(self, k: str) -> Any:
        try:
            v = self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc
        return Config(v) if isinstance(v, dict) else v


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_home() -> Path:
    """数据根目录：RELAY_HOME 优先；exe 运行用 %LOCALAPPDATA%/RelayConsole；源码运行用仓库根。"""
    env = os.environ.get("RELAY_HOME")
    if env:
        return Path(env)
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "RelayConsole"
    return ROOT


def config_path() -> Path:
    return data_home() / "config.yaml"


def _deep_fill(base: dict, defaults: dict) -> dict:
    """用 defaults 补齐 base 缺失的键（递归；只补缺，绝不覆盖已有值）。"""
    for k, v in defaults.items():
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_fill(base[k], v)
    return base


def _starter_config(home: Path) -> None:
    """exe 首次运行：在数据根生成 config.yaml，数据路径固定在数据根内（程序/数据彻底分离）。"""
    home.mkdir(parents=True, exist_ok=True)   # 数据根本身可能还不存在
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    example["data_dir"] = str(home / "data")
    example["profiles_dir"] = str(home / "profiles")
    header = ("# Relay Console 数据根目录配置（自动生成）\n"
              f"# 程序版本 v{APP_VERSION}；升级/更换程序不影响本目录：config.yaml、data/、profiles/ 都在这里。\n")
    (home / "config.yaml").write_text(
        header + yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _chrome_candidates() -> list[Path]:
    """Windows 上常见的 Chrome 安装位置，按优先级排列。"""
    out: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Google/Chrome/Application/chrome.exe")
    return out


def find_chrome() -> str | None:
    """返回第一个存在的 Chrome 路径；都没有则 None。"""
    for candidate in _chrome_candidates():
        if candidate.exists():
            return str(candidate)
    return None


def load_config(path: str | Path | None = None) -> Config:
    home = data_home()
    p = Path(path) if path else config_path()
    if not p.exists():
        if path is None and is_frozen():
            _starter_config(home)          # exe 首次运行，生成初始配置
        else:
            p = ROOT / "config.example.yaml"
    cfg = Config(yaml.safe_load(p.read_text(encoding="utf-8")))
    # 升级继承：老配置缺失的新键从模板补齐（不覆盖已有值）
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    _deep_fill(cfg, example)
    # chrome_path 为空或不存在时自动探测；都找不到就保留原值，交给启动时报 ChromeNotFound
    if not cfg.get("chrome_path") or not Path(str(cfg["chrome_path"])).exists():
        detected = find_chrome()
        if detected:
            cfg["chrome_path"] = detected
    # 相对路径的落点：RELAY_HOME/exe 运行用数据根，源码运行保持仓库根（老用户不受影响）
    base = home if (os.environ.get("RELAY_HOME") or is_frozen()) else ROOT
    for key in ("data_dir", "profiles_dir"):
        d = Path(cfg[key])
        if not d.is_absolute():
            d = base / d
        d.mkdir(parents=True, exist_ok=True)
        cfg[key] = str(d)
    return cfg


def platform_accounts(cfg, pid: str) -> list[str]:
    """某平台配置的账号列表；未配置 accounts 时视为单账号 main。"""
    p = cfg.get("platforms", {}).get(pid) or {}
    accounts = p.get("accounts") or ["main"]
    return list(accounts)


def _portable_path(value: str) -> str:
    """把解析后的绝对路径写回成相对数据根的路径，配置才能随数据目录一起搬走。"""
    p = Path(value)
    for base in (ROOT, data_home()):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        return f"./{rel.as_posix()}"
    return value


def save_config(cfg) -> None:
    """把当前生效配置整体原子写回 config.yaml（yaml 注释不保留）。"""
    data = dict(cfg)
    for key in ("data_dir", "profiles_dir"):
        if key in data:
            data[key] = _portable_path(str(data[key]))
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, p)      # 同目录原子替换：写一半断电也不会留下半截配置
