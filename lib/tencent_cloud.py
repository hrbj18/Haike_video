"""Minimal TC3-HMAC-SHA256 client for Tencent Cloud speech APIs.

Haike Video talks to Tencent Cloud 语音技术 (TTS / ASR) over raw HTTPS so the
project does not need to vendor the full Tencent Cloud SDK.  Signing follows
the official TC3-HMAC-SHA256 specification exactly.

Security contract: credentials are only ever read from the process
environment (populated from ``.env.secrets.local``) and no helper in this
module ever returns or logs the SecretKey — every user-visible error is passed
through :func:`redact_secret` first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lib.env_loader import load_env


TTS_SERVICE = "tts"
TTS_VERSION = "2019-08-23"
TTS_HOST = "tts.tencentcloudapi.com"

ASR_SERVICE = "asr"
ASR_VERSION = "2019-06-14"
ASR_HOST = "asr.tencentcloudapi.com"

DEFAULT_REGION = "ap-guangzhou"
SUPPORTED_REGIONS: tuple[str, ...] = (
    "ap-guangzhou",
    "ap-shanghai",
    "ap-beijing",
    "ap-chengdu",
    "ap-chongqing",
    "ap-nanjing",
    "ap-hongkong",
)

SECRET_ID_ENV = "TENCENT_SECRET_ID"
SECRET_KEY_ENV = "TENCENT_SECRET_KEY"
TTS_REGION_ENV = "TENCENT_TTS_REGION"
ASR_REGION_ENV = "TENCENT_ASR_REGION"

# Tencent Cloud returns this Error.Code when the account has never opened the
# product in the console.  It is user-remediable, not a credential problem.
NOT_OPENED_CODE = "FailedOperation.UserNotRegistered"


class TencentCloudError(RuntimeError):
    """A user-correctable Tencent Cloud failure."""

    def __init__(self, message: str, *, code: str = "", request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class TencentCredentials:
    secret_id: str
    secret_key: str

    @property
    def configured(self) -> bool:
        return bool(self.secret_id and self.secret_key)


def tencent_credentials() -> TencentCredentials:
    """Read the account credentials currently effective for this process."""
    load_env()
    return TencentCredentials(
        secret_id=str(os.environ.get(SECRET_ID_ENV) or "").strip(),
        secret_key=str(os.environ.get(SECRET_KEY_ENV) or "").strip(),
    )


def tencent_region(service: str) -> str:
    """Resolve the per-service region with a sane default."""
    load_env()
    variable = ASR_REGION_ENV if service == ASR_SERVICE else TTS_REGION_ENV
    region = str(os.environ.get(variable) or "").strip()
    return region or DEFAULT_REGION


def mask_secret(value: str) -> str:
    """Render a credential for the UI without revealing it."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "已保存"
    return f"{text[:4]}••••••{text[-4:]}"


def redact_secret(message: object, credentials: TencentCredentials | None = None) -> str:
    """Strip any known credential from text that may reach the user or logs."""
    text = str(message or "")
    creds = credentials or tencent_credentials()
    for secret in (creds.secret_key, creds.secret_id):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:900]


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def tc3_request(
    service: str,
    action: str,
    payload: dict[str, Any],
    *,
    version: str,
    region: str | None = None,
    host: str | None = None,
    timeout: float = 60.0,
    requests_module: Any | None = None,
) -> dict[str, Any]:
    """Call one Tencent Cloud API action and return the raw JSON response.

    The response body is returned as-is (including a possible ``Response.Error``)
    so callers can map product-specific error codes to actionable messages.
    Transport and authentication problems raise :class:`TencentCloudError`.
    """
    credentials = tencent_credentials()
    if not credentials.configured:
        raise TencentCloudError(
            "尚未配置腾讯云 SecretId / SecretKey；请在「配音中心 → API 管理」中填写并保存。"
        )

    resolved_host = host or f"{service}.tencentcloudapi.com"
    resolved_region = region or tencent_region(service)
    if requests_module is None:  # pragma: no cover - import guard
        import requests as requests_module

    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    hashed_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{resolved_host}\n"
        f"x-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        hashed_payload,
    ])

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    secret_date = _sign(("TC3" + credentials.secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={credentials.secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": resolved_host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": resolved_region,
    }

    try:
        response = requests_module.post(
            f"https://{resolved_host}",
            headers=headers,
            data=payload_str.encode("utf-8"),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a user-facing error
        raise TencentCloudError(
            f"腾讯云 {service} 网络请求失败：{redact_secret(exc, credentials)}"
        ) from exc

    try:
        parsed = response.json()
    except ValueError as exc:
        raise TencentCloudError(
            f"腾讯云 {service} 返回了非 JSON 响应（HTTP {getattr(response, 'status_code', '?')}）"
        ) from exc
    if not isinstance(parsed, dict):
        raise TencentCloudError(f"腾讯云 {service} 返回了意外的响应结构")
    return parsed


def response_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``Response.Error`` when the API reported a failure."""
    response = payload.get("Response")
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return error
    return None


def response_request_id(payload: dict[str, Any]) -> str:
    response = payload.get("Response")
    if isinstance(response, dict):
        return str(response.get("RequestId") or "")
    return ""


def friendly_error(code: str, message: str, *, product: str) -> str:
    """Map Tencent Cloud error codes to a Chinese, actionable message."""
    detail = redact_secret(message).strip()
    if code in {NOT_OPENED_CODE, "FailedOperation.NotRegistered"}:
        return (
            f"当前腾讯云账号尚未开通「{product}」服务，请到腾讯云控制台完成开通后重试"
            f"（原始信息：{detail}）"
        )
    if code.startswith("AuthFailure"):
        return f"腾讯云密钥校验失败：{detail}。请确认 SecretId / SecretKey 正确且未被禁用。"
    if code.startswith("UnauthorizedOperation") or code == "FailedOperation":
        return f"腾讯云账号无权调用「{product}」：{detail}"
    if code.startswith("RequestLimitExceeded") or code == "FailedOperation.ServiceBusy":
        return f"腾讯云「{product}」请求过于频繁，请稍后重试：{detail}"
    if code.startswith("InvalidParameter"):
        return f"腾讯云「{product}」参数不合法：{detail}"
    if code.startswith("LimitExceeded") or "Quota" in code:
        return f"腾讯云「{product}」配额已用尽：{detail}"
    return f"腾讯云「{product}」调用失败（{code or 'unknown'}）：{detail}"
