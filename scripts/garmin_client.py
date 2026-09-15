#!/usr/bin/env python3
"""
Garmin Connect 客户端封装（凭据解析 / token 缓存 / MFA / 指数退避重试）。

本 skill 里所有需要访问 Garmin 的脚本都通过 `from garmin_client import get_client` 拿客户端，
避免每个脚本重复实现登录逻辑。

凭据来源优先级：
  1. 环境变量 GARMIN_EMAIL / GARMIN_PASSWORD / GARMIN_IS_CN
  2. 本 skill 根目录的 .env
  3. 已安装的其他 skill 的 .env（如 run-coach__skillhub，自动发现，零配置复用）

token 缓存目录：本 skill 根目录的 .garth（与 run-coach 相互独立，互不覆盖）。
"""

import contextlib
import os
import sys
import time
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
GARTH_HOME = str(SKILL_ROOT / ".garth")
ENV_FILE = SKILL_ROOT / ".env"

# 自动发现的其他 .env 位置（按优先级）
FALLBACK_ENV_FILES = [
    Path.home() / ".workbuddy" / "skills" / "run-coach__skillhub" / ".env",
    Path.home() / ".workbuddy" / "skills" / "coros-mcp-energy-lab__skillhub" / ".env",
]


def _read_env_file(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _env_or_envfile(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    for path in [ENV_FILE] + FALLBACK_ENV_FILES:
        val = _read_env_file(path, key)
        if val:
            return val
    return ""


def load_credentials():
    email = _env_or_envfile("GARMIN_EMAIL")
    password = _env_or_envfile("GARMIN_PASSWORD")
    if not email or not password:
        print("错误：缺少 Garmin 凭据。请在本 skill 根目录创建 .env：")
        print("  GARMIN_EMAIL=your@email.com")
        print("  GARMIN_PASSWORD=your_password")
        print("  GARMIN_IS_CN=1            # 中国区账号（connect.garmin.cn）")
        sys.exit(1)
    return email, password


def is_cn_account() -> bool:
    return _env_or_envfile("GARMIN_IS_CN").strip().lower() in ("1", "true", "yes", "cn")


def get_client():
    """登录 Garmin Connect，优先使用缓存 token。"""
    email, password = load_credentials()
    use_cn = is_cn_account()
    client = Garmin(email=email, password=password, is_cn=use_cn)

    try:
        mfa_status, _legacy = client.login(GARTH_HOME)
        if mfa_status:
            print("Garmin 需要短信/邮箱验证码：")
            code = input("请输入验证码: ").strip()
            client.resume_login(mfa_status, code)
            with contextlib.suppress(Exception):
                client.client.dump(GARTH_HOME)
            print("MFA 登录成功，token 已缓存。")
        else:
            print(f"已登录 Garmin Connect（中国区={use_cn}，token 已缓存）。")
    except GarminConnectAuthenticationError as e:
        print(f"认证失败：{e}")
        print("提示：若账号开启了 MFA，请删除 .garth 目录后重试以重新触发验证码流程。")
        sys.exit(1)
    except GarminConnectConnectionError as e:
        print(f"连接错误：{e}")
        sys.exit(1)
    except GarminConnectTooManyRequestsError as e:
        print(f"触发限流（429）：{e}\n请等待几分钟后重试。")
        sys.exit(1)

    return client


def safe_call(fn, *args, retries=3, default=None, label="", **kwargs):
    """
    调用 Garmin 接口并吞掉异常（返回 default）。
    - 429 限流时指数退避重试（2s / 6s / 18s）
    - 其它异常直接降级，不中断整条流水线（Garmin 很多端点对老设备返回 404）
    """
    last_err = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError as e:
            last_err = e
            wait = 2 * (3 ** attempt)
            print(f"  [限流] {label or fn.__name__}，{wait}s 后重试（{attempt + 1}/{retries}）")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001 - 单点降级，不希望中断
            last_err = e
            break
    if label:
        print(f"  [跳过] {label}: {last_err}")
    return default


def dump_token(client):
    with contextlib.suppress(Exception):
        client.client.dump(GARTH_HOME)
