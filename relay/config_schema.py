"""配置校验层（B1）。

所有新的配置接口先过这层：失败返回 400 + 字段级中文错误列表（[{"field", "message"}]），
前端把 message 显示在对应输入框旁边。规则：
  - 平台端口 1024-65535；全局端口冲突沿用 ChromeManager 的 base+10*i 规则
  - 平台 url 必须 https
  - 账号名字符集与 Agent 名字一致（中英文、数字、下划线、点、连字符，1-32 位）
  - fingerprint.backend 只能是 chrome / adspower / ovobrowser 之一
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import pydantic

# 与 ArenaPool._NAME_RE 一致：账号名与 Agent 名字使用同一字符集
ACCOUNT_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fa5.-]{1,32}")

BACKENDS = ("chrome", "adspower", "ovobrowser")

# pydantic 内置类型错误的中文翻译（自定义校验器抛的 ValueError 本身就是中文）
_TYPE_ZH = {
    "int_type": "必须是整数",
    "int_parsing": "必须是整数",
    "float_type": "必须是数字",
    "float_parsing": "必须是数字",
    "bool_type": "必须是布尔值",
    "string_type": "必须是文本",
    "missing": "缺少必填字段",
}


def pydantic_errors(exc: pydantic.ValidationError, default_field: str = "") -> list[dict]:
    """把 pydantic 校验错误转成字段级中文错误列表。"""
    out = []
    for err in exc.errors():
        field = ".".join(str(x) for x in err["loc"]) or default_field
        msg = _TYPE_ZH.get(err["type"]) or err["msg"]
        if msg.startswith("Value error, "):          # pydantic 对 ValueError 的英文包装
            msg = msg[len("Value error, "):]
        out.append({"field": field, "message": msg})
    return out


def _fail(field: str, message: str) -> dict:
    return {"field": field, "message": message}


class PlatformModel(pydantic.BaseModel):
    """platforms.<pid> 段的配置模型。"""

    enabled: bool = True
    url: str | None = None
    port: int
    accounts: list[str] = ["main"]
    autofill: bool = False

    @pydantic.field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not 1024 <= int(v) <= 65535:
            raise ValueError("端口必须在 1024-65535 之间")
        return v

    @pydantic.field_validator("url")
    @classmethod
    def _url_https(cls, v):
        if v is None or v == "":
            return None
        u = urlparse(str(v))
        if u.scheme != "https" or not u.hostname:
            raise ValueError("平台 url 必须是 https 地址")
        return v

    @pydantic.field_validator("accounts")
    @classmethod
    def _accounts(cls, v):
        if not v:
            raise ValueError("至少保留一个账号")
        for a in v:
            if not isinstance(a, str) or not ACCOUNT_RE.fullmatch(a):
                raise ValueError("账号名只能用中英文、数字、下划线、点、连字符，长度 1-32")
        if len(set(v)) != len(v):
            raise ValueError("账号名不能重复")
        return v


class FingerprintModel(pydantic.BaseModel):
    """fingerprint 段的配置模型。api_key 校验后由接口层只写进系统凭据库，不落盘。"""

    backend: str = "chrome"
    api_base: str = ""
    api_key: str = ""
    startup_timeout_seconds: float = 90
    profiles: dict[str, dict[str, str]] = {}

    @pydantic.field_validator("backend")
    @classmethod
    def _backend(cls, v: str) -> str:
        if v not in BACKENDS:
            raise ValueError(f"backend 必须是 {'、'.join(BACKENDS)} 之一")
        return v

    @pydantic.field_validator("api_base")
    @classmethod
    def _api_base(cls, v: str) -> str:
        if not v:
            return ""
        u = urlparse(str(v))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("api_base 必须是 http(s) 地址（指纹浏览器本地 API）")
        return v

    @pydantic.field_validator("startup_timeout_seconds")
    @classmethod
    def _timeout(cls, v):
        if not 1 <= float(v) <= 600:
            raise ValueError("startup_timeout_seconds 必须在 1-600 秒之间")
        return float(v)

    @pydantic.model_validator(mode="after")
    def _profile_dup(self):
        """同一个环境 ID 被两个「平台:账号」选中时报错。"""
        seen: dict[str, str] = {}
        for pid, per in (self.profiles or {}).items():
            for account, env in (per or {}).items():
                key = str(env)
                if key in seen:
                    raise ValueError(f"环境 {key} 同时分配给了 {seen[key]} 和 {pid}:{account}，一个环境只能对应一个账号")
                seen[key] = f"{pid}:{account}"
        return self


def validate_platform(pid: str, data: dict) -> tuple[dict | None, list[dict]]:
    """校验单个平台的配置更新，返回 (清洗后的数据, 字段级错误列表)。"""
    try:
        m = PlatformModel(**(data or {}))
    except pydantic.ValidationError as e:
        return None, [{"field": f"platforms.{pid}.{x['field']}", "message": x["message"]}
                      for x in pydantic_errors(e)]
    return m.model_dump(), []


def validate_fingerprint(data: dict) -> tuple[dict | None, list[dict]]:
    """校验 fingerprint 段的配置更新，返回 (清洗后的数据, 字段级错误列表)。"""
    try:
        m = FingerprintModel(**(data or {}))
    except pydantic.ValidationError as e:
        return None, [{"field": f"fingerprint.{x['field']}", "message": x["message"]}
                      for x in pydantic_errors(e, default_field="profiles")]
    return m.model_dump(), []


def port_conflicts(platforms: dict) -> list[str]:
    """全局端口冲突检测（与 ChromeManager 相同的 base+10*i 规则）。返回中文冲突描述列表。"""
    seen: dict[int, tuple[str, str]] = {}
    out: list[str] = []
    for pid, p in (platforms or {}).items():
        if not p:
            continue
        base = p.get("port")
        if base is None:
            continue
        accounts = p.get("accounts") or ["main"]
        for i, account in enumerate(accounts[:10]):
            port = int(base) + 10 * i
            if port in seen:
                out.append(f"{seen[port][0]}:{seen[port][1]} 与 {pid}:{account} 都使用端口 {port}")
            else:
                seen[port] = (pid, account)
    return out
