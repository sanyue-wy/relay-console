"""B1 配置校验层：字段级中文错误、端口冲突复用、指纹环境重复分配。"""
import sys
from pathlib import Path

import pytest
import pydantic

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.browser import ChromeManager  # noqa: E402
from relay.config_schema import (FingerprintModel, PlatformModel, port_conflicts,  # noqa: E402
                                 pydantic_errors, validate_fingerprint, validate_platform)


def _first_msg(exc: pydantic.ValidationError, field: str | None = None) -> str:
    errs = pydantic_errors(exc)
    if field:
        errs = [e for e in errs if e["field"].endswith(field)]
    assert errs, f"应存在校验错误: {pydantic_errors(exc)}"
    return errs[0]["message"]


def _raises(model, **kw):
    return pytest.raises(pydantic.ValidationError)


def test_port_range():
    assert PlatformModel(port=1024).port == 1024
    assert PlatformModel(port=65535).port == 65535
    for bad in (1023, 65536, -1):
        with pytest.raises(pydantic.ValidationError) as ei:
            PlatformModel(port=bad)
        assert "1024-65535" in _first_msg(ei.value, "port")


def test_port_type_translated():
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port="abc")
    assert "整数" in _first_msg(ei.value, "port")


def test_url_https_only():
    assert PlatformModel(port=9301, url="https://hark.example.com").url
    assert PlatformModel(port=9301).url is None
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port=9301, url="http://hark.example.com")
    assert "https" in _first_msg(ei.value, "url")
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port=9301, url="notaurl")
    assert "https" in _first_msg(ei.value, "url")


def test_account_charset():
    ok = PlatformModel(port=9301, accounts=["main", "alt-1", "小号.2"])
    assert ok.accounts == ["main", "alt-1", "小号.2"]
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port=9301, accounts=[])
    assert "至少保留一个账号" in _first_msg(ei.value, "accounts")
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port=9301, accounts=["a", "a"])
    assert "不能重复" in _first_msg(ei.value, "accounts")
    with pytest.raises(pydantic.ValidationError) as ei:
        PlatformModel(port=9301, accounts=["坏 名字!"])
    assert "账号名" in _first_msg(ei.value, "accounts")


def test_fingerprint_backend_and_api_base():
    for b in ("chrome", "adspower", "ovobrowser"):
        assert FingerprintModel(backend=b).backend == b
    with pytest.raises(pydantic.ValidationError) as ei:
        FingerprintModel(backend="camoufox")
    assert "之一" in _first_msg(ei.value, "backend")
    assert FingerprintModel(api_base="").api_base == ""
    assert FingerprintModel(api_base="http://127.0.0.1:50325").api_base
    with pytest.raises(pydantic.ValidationError) as ei:
        FingerprintModel(api_base="ftp://x")
    assert "http(s)" in _first_msg(ei.value, "api_base")
    with pytest.raises(pydantic.ValidationError) as ei:
        FingerprintModel(startup_timeout_seconds=0)
    assert "1-600" in _first_msg(ei.value, "startup_timeout_seconds")


def test_fingerprint_profile_dup_env():
    ok = FingerprintModel(backend="adspower",
                          profiles={"deepseek": {"main": "E1"}, "kimi": {"main": "E2"}})
    assert ok.profiles["kimi"]["main"] == "E2"
    with pytest.raises(pydantic.ValidationError) as ei:
        FingerprintModel(backend="adspower",
                         profiles={"deepseek": {"main": "E1"}, "kimi": {"main": "E1"}})
    assert any("同时分配" in e["message"] for e in pydantic_errors(ei.value))
    # 走 validate_fingerprint 时落到 fingerprint.profiles 字段
    _, errs = validate_fingerprint({"backend": "adspower",
                                    "profiles": {"deepseek": {"main": "E1"}, "kimi": {"main": "E1"}}})
    assert errs and errs[0]["field"] == "fingerprint.profiles" and "同时分配" in errs[0]["message"]


def test_validate_helpers_field_prefix():
    ok, errs = validate_platform("deepseek", {"port": 9302})
    assert ok["port"] == 9302 and errs == []
    _, errs = validate_platform("deepseek", {"port": 80})
    assert errs[0]["field"] == "platforms.deepseek.port"
    ok, errs = validate_fingerprint({"backend": "adspower", "startup_timeout_seconds": 120})
    assert ok["startup_timeout_seconds"] == 120 and errs == []
    _, errs = validate_fingerprint({"backend": "x"})
    assert errs[0]["field"].startswith("fingerprint.")


def test_port_conflicts_rule():
    assert port_conflicts({
        "deepseek": {"port": 9301, "accounts": ["main", "alt"]},   # 9301, 9311
        "doubao": {"port": 9311, "accounts": ["main"]},
    }) == ["deepseek:alt 与 doubao:main 都使用端口 9311"]
    assert port_conflicts({"deepseek": {"port": 9301}, "doubao": {"port": 9302}}) == []


def test_chrome_manager_uses_same_conflict_rule(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="9311"):
        ChromeManager({"platforms": {
            "deepseek": {"port": 9301, "accounts": ["main", "alt"]},
            "doubao": {"port": 9311, "accounts": ["main"]}},
            "profiles_dir": str(tmp_path / "p"), "chrome_path": str(chrome),
            "fingerprint": {"backend": "chrome"}})


def test_accounts_type_error_has_message():
    with pytest.raises(pydantic.ValidationError):
        PlatformModel(port=9301, accounts="main")
