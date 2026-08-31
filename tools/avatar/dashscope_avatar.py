"""Native DashScope WAN S2V avatar provider.

This provider deliberately uses DashScope's native asynchronous REST API,
including temporary OSS upload and immediate result download.  It does not use
an OpenAI-compatible gateway because media task semantics are different.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool, DependencyError, Determinism, ExecutionMode, ResourceProfile,
    RetryPolicy, ToolResult, ToolRuntime, ToolStability, ToolTier,
)


class DashscopeAvatarError(RuntimeError):
    """A provider response that can be safely displayed to a user."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DashscopeWanS2VClient:
    """Small native client kept separate so Backlot can persist async stages."""

    upload_policy_url = "https://dashscope.aliyuncs.com/api/v1/uploads"
    detect_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2video/face-detect"

    def __init__(self, *, api_key: str | None = None, workspace_id: str | None = None, session: requests.Session | None = None):
        self.api_key = str(api_key or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
        self.workspace_id = str(workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID") or "").strip()
        if not self.api_key:
            raise DashscopeAvatarError("尚未配置 DASHSCOPE_API_KEY，无法调用阿里云数字人服务")
        if not self.workspace_id:
            raise DashscopeAvatarError("尚未配置 DASHSCOPE_WORKSPACE_ID（北京地域工作空间 ID）")
        self.session = session or requests.Session()
        self.workspace_base = f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1"

    def _headers(self, *, async_task: bool = False, oss_resource: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if async_task:
            headers["X-DashScope-Async"] = "enable"
        if oss_resource:
            headers["X-DashScope-OssResourceResolve"] = "enable"
        return headers

    @staticmethod
    def _error(response: requests.Response, action: str) -> DashscopeAvatarError:
        try:
            body = response.json()
            message = body.get("message") or body.get("code") or body.get("detail") or str(body)
        except ValueError:
            message = response.text[-500:] or f"HTTP {response.status_code}"
        return DashscopeAvatarError(f"阿里云{action}失败：{message}")

    def _json(self, response: requests.Response, action: str) -> dict[str, Any]:
        if not response.ok:
            raise self._error(response, action)
        try:
            value = response.json()
        except ValueError as exc:
            raise DashscopeAvatarError(f"阿里云{action}返回了无效数据") from exc
        if not isinstance(value, dict):
            raise DashscopeAvatarError(f"阿里云{action}返回了无效数据")
        return value

    def upload_file(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise DashscopeAvatarError("待上传的数字人输入文件不存在")
        policy_response = self.session.get(
            self.upload_policy_url,
            params={"action": "getPolicy", "model": "wan2.2-s2v"},
            headers=self._headers(),
            timeout=45,
        )
        policy_response_data = self._json(policy_response, "临时文件授权")
        # The native uploads endpoint returns the policy directly in some
        # accounts, while the current Bailian response wraps the same fields
        # in ``data``.  Keep both shapes compatible, without persisting any
        # short-lived credentials in the project package or logs.
        nested_policy = policy_response_data.get("data")
        policy = nested_policy if isinstance(nested_policy, dict) else policy_response_data
        required = ("upload_host", "upload_dir", "oss_access_key_id", "policy", "signature")
        if any(not policy.get(item) for item in required):
            raise DashscopeAvatarError("阿里云临时文件授权缺少必要字段")
        object_key = f"{str(policy['upload_dir']).rstrip('/')}/{path.name}"
        form = {
            "OSSAccessKeyId": str(policy["oss_access_key_id"]),
            "Signature": str(policy["signature"]),
            "policy": str(policy["policy"]),
            "key": object_key,
            "x-oss-object-acl": str(policy.get("x_oss_object_acl") or "private"),
            "x-oss-forbid-overwrite": str(policy.get("x_oss_forbid_overwrite") or "true"),
            "success_action_status": "200",
        }
        with path.open("rb") as source:
            upload_response = self.session.post(
                str(policy["upload_host"]),
                data=form,
                files={"file": (path.name, source)},
                timeout=120,
            )
        if upload_response.status_code not in {200, 201, 204}:
            raise self._error(upload_response, "临时文件上传")
        return {
            "oss_url": f"oss://{object_key}",
            "expires_at": (datetime.now(UTC) + timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    def detect_face(self, image_url: str) -> dict[str, Any]:
        response = self.session.post(
            self.detect_url,
            headers=self._headers(oss_resource=image_url.startswith("oss://")),
            json={"model": "wan2.2-s2v-detect", "input": {"image_url": image_url}},
            timeout=90,
        )
        value = self._json(response, "出镜图检测")
        output = value.get("output") if isinstance(value.get("output"), dict) else {}
        if output.get("check_pass") is not True:
            raise DashscopeAvatarError("出镜图未通过阿里云人像检测；请上传单人、清晰、正面或半身的项目出镜图")
        return value

    def submit(self, image_url: str, audio_url: str, *, resolution: str = "480P") -> dict[str, Any]:
        if resolution not in {"480P", "720P"}:
            raise DashscopeAvatarError("阿里云数字人清晰度只能选择 480P 或 720P")
        use_oss = image_url.startswith("oss://") or audio_url.startswith("oss://")
        response = self.session.post(
            f"{self.workspace_base}/services/aigc/image2video/video-synthesis",
            headers=self._headers(async_task=True, oss_resource=use_oss),
            json={
                "model": "wan2.2-s2v",
                "input": {"image_url": image_url, "audio_url": audio_url},
                "parameters": {"resolution": resolution},
            },
            timeout=90,
        )
        value = self._json(response, "数字人任务提交")
        output = value.get("output") if isinstance(value.get("output"), dict) else {}
        task_id = output.get("task_id") or value.get("task_id")
        if not task_id:
            raise DashscopeAvatarError("阿里云未返回数字人任务编号")
        return {"task_id": str(task_id), "raw": value}

    def poll(self, task_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.workspace_base}/tasks/{task_id}",
            headers=self._headers(),
            timeout=45,
        )
        value = self._json(response, "数字人任务查询")
        output = value.get("output") if isinstance(value.get("output"), dict) else {}
        status = str(output.get("task_status") or output.get("status") or "UNKNOWN").upper()
        video_url = output.get("video_url") or output.get("url")
        return {"status": status, "video_url": str(video_url) if video_url else None, "raw": value}

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        response = self.session.get(url, stream=True, timeout=300)
        if not response.ok:
            raise self._error(response, "生成结果下载")
        with target.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
        if not target.is_file() or target.stat().st_size <= 0:
            raise DashscopeAvatarError("阿里云生成结果为空")


class DashscopeAvatar(BaseTool):
    name = "dashscope_avatar"
    version = "1.0.0"
    tier = ToolTier.GENERATE
    capability = "avatar"
    provider = "dashscope"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies = ["env:DASHSCOPE_API_KEY", "env:DASHSCOPE_WORKSPACE_ID"]
    install_instructions = "在 .env.secrets.local 中配置 DASHSCOPE_API_KEY 与 DASHSCOPE_WORKSPACE_ID。"
    agent_skills = ["dashscope", "avatar-video"]
    capabilities = ["avatar_video", "audio_driven_avatar", "native_async_task"]
    supports = {"audio_driven_animation": True, "cloud_render": True, "offline": False}
    best_for = ["单人出镜图加 20 秒以内中文驱动音频", "按片段可恢复生成"]
    not_good_for = ["多人物合照", "超过 20 秒的单段音频", "未确认的批量付费任务"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=600, network_required=True)
    retry_policy = RetryPolicy(max_retries=1, backoff_seconds=5.0, retryable_errors=["429", "500", "503"])
    side_effects = ["调用阿里云付费异步数字人生成", "写入本地 MP4 结果"]
    user_visible_verification = ["试听原声与口型", "检查出镜图身份一致性"]
    quality_score = 0.78
    latency_p50_seconds = 420.0
    input_schema = {
        "type": "object",
        "required": ["image_path", "audio_path", "output_path"],
        "properties": {
            "image_path": {"type": "string"}, "audio_path": {"type": "string"},
            "output_path": {"type": "string"}, "resolution": {"type": "string", "enum": ["480P", "720P"], "default": "480P"},
            "timeout_seconds": {"type": "integer", "default": 1200}, "poll_interval": {"type": "number", "default": 15},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
            client = DashscopeWanS2VClient()
            image = client.upload_file(Path(str(inputs["image_path"])))
            audio = client.upload_file(Path(str(inputs["audio_path"])))
            client.detect_face(str(image["oss_url"]))
            submitted = client.submit(str(image["oss_url"]), str(audio["oss_url"]), resolution=str(inputs.get("resolution") or "480P"))
            deadline = time.monotonic() + int(inputs.get("timeout_seconds") or 1200)
            while time.monotonic() < deadline:
                status = client.poll(submitted["task_id"])
                if status["status"] == "SUCCEEDED" and status.get("video_url"):
                    target = Path(str(inputs["output_path"]))
                    client.download(str(status["video_url"]), target)
                    return ToolResult(success=True, data={"task_id": submitted["task_id"], "output_path": str(target), "completed_at": _now()}, artifacts=[str(target)], model="wan2.2-s2v")
                if status["status"] in {"FAILED", "CANCELED", "UNKNOWN"}:
                    raise DashscopeAvatarError(f"阿里云任务状态为 {status['status']}")
                time.sleep(max(1.0, float(inputs.get("poll_interval") or 15)))
            raise DashscopeAvatarError("等待阿里云数字人任务超时，可在工作台中继续跟踪")
        except (DashscopeAvatarError, DependencyError, OSError, KeyError) as exc:
            return ToolResult(success=False, data={"provider": self.provider}, error=str(exc))
