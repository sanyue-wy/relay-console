"""配置层自测：数据根目录、老配置升级补齐、exe 首次运行引导。"""
import sys

import yaml

from relay.config import _deep_fill, data_home, load_config


def test_deep_fill_only_adds_missing():
    old = {"host": "1.2.3.4", "summary": {"model": "m1"}}
    merged = _deep_fill(old, {"host": "0.0.0.0", "summary": {"model": "m2", "timeout_seconds": 9},
                              "fingerprint": {"backend": "chrome"}})
    assert merged["host"] == "1.2.3.4"                    # 不覆盖已有值
    assert merged["summary"]["model"] == "m1" and merged["summary"]["timeout_seconds"] == 9
    assert merged["fingerprint"]["backend"] == "chrome"   # 新键补齐


def test_relay_home_and_upgrade_merge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RELAY_HOME", home.as_posix())
    assert data_home() == home
    # 老版本配置：没有 fingerprint/arena 段，data_dir 是相对路径
    old = {"host": "127.0.0.1", "port": 8801, "data_dir": "./data", "profiles_dir": "./profiles",
           "summary": {"model": "deepseek-chat"}}
    (home / "config.yaml").write_text(yaml.safe_dump(old), encoding="utf-8")
    cfg = load_config()
    assert cfg["fingerprint"]["backend"] == "chrome"       # 缺失键自动补齐（升级继承）
    assert cfg["arena"]["agent_turn_timeout"] == 600
    assert cfg["data_dir"] == str(home / "data")           # 相对路径落在数据根下
    assert (home / "data").exists()


def test_frozen_first_run_bootstrap(tmp_path, monkeypatch):
    home = tmp_path / "appdata"
    monkeypatch.setenv("RELAY_HOME", str(home))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = load_config()                                    # 无 config.yaml → exe 首次运行自动生成
    assert (home / "config.yaml").exists()
    assert cfg["data_dir"] == str(home / "data") and cfg["profiles_dir"] == str(home / "profiles")
    assert cfg["platforms"]["deepseek"]["port"] == 9301
    # 配置里不落任何密钥/凭据（api_key 已移入系统凭据库，模板中不再有该字段）
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "api_key" not in text
    assert cfg["platforms"]["deepseek"].get("enabled") is True     # enabled 键随模板补齐
