"""Native DashScope WAN S2V request contract tests (no network)."""

from __future__ import annotations

from pathlib import Path

from tools.avatar.dashscope_avatar import DashscopeWanS2VClient


class Response:
    def __init__(self, body: dict, status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = ""

    def json(self):
        return self.body


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if "uploads" in url:
            return Response({
                "upload_host": "https://oss.example/upload", "upload_dir": "tmp/path",
                "oss_access_key_id": "access", "policy": "policy", "signature": "signature",
                "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true",
            })
        if "/tasks/" in url:
            return Response({"output": {"task_status": "SUCCEEDED", "video_url": "https://result.example/video.mp4"}})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if "oss.example" in url:
            return Response({})
        if "face-detect" in url:
            return Response({"output": {"check_pass": True, "humanoid": True}})
        if "video-synthesis" in url:
            return Response({"output": {"task_id": "task-123"}})
        raise AssertionError(url)


class NestedPolicySession(Session):
    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if "uploads" in url:
            return Response({"data": {
                "upload_host": "https://oss.example/upload", "upload_dir": "tmp/path",
                "oss_access_key_id": "access", "policy": "policy", "signature": "signature",
            }})
        return super().get(url, **kwargs)


def test_wan_s2v_client_uses_native_workspace_endpoints_and_oss_resolution(tmp_path):
    source = tmp_path / "presenter.png"
    source.write_bytes(b"image")
    session = Session()
    client = DashscopeWanS2VClient(api_key="secret", workspace_id="workspace-id", session=session)

    uploaded = client.upload_file(source)
    assert uploaded["oss_url"] == "oss://tmp/path/presenter.png"
    upload_call = session.calls[-1]
    assert upload_call[0] == "post"
    assert upload_call[2]["data"]["key"] == "tmp/path/presenter.png"

    client.detect_face(uploaded["oss_url"])
    detect_call = session.calls[-1]
    assert detect_call[2]["headers"]["X-DashScope-OssResourceResolve"] == "enable"

    submitted = client.submit(uploaded["oss_url"], "oss://tmp/path/audio.mp3", resolution="480P")
    submit_call = session.calls[-1]
    assert submitted["task_id"] == "task-123"
    assert submit_call[1].startswith("https://workspace-id.cn-beijing.maas.aliyuncs.com/api/v1/")
    assert submit_call[2]["headers"]["X-DashScope-Async"] == "enable"
    assert submit_call[2]["json"]["model"] == "wan2.2-s2v"

    result = client.poll("task-123")
    assert result == {"status": "SUCCEEDED", "video_url": "https://result.example/video.mp4", "raw": {"output": {"task_status": "SUCCEEDED", "video_url": "https://result.example/video.mp4"}}}


def test_wan_s2v_client_accepts_nested_upload_policy(tmp_path):
    source = tmp_path / "presenter.png"
    source.write_bytes(b"image")
    session = NestedPolicySession()
    client = DashscopeWanS2VClient(api_key="secret", workspace_id="workspace-id", session=session)

    uploaded = client.upload_file(source)

    assert uploaded["oss_url"] == "oss://tmp/path/presenter.png"
    upload_call = session.calls[-1]
    assert upload_call[2]["data"]["OSSAccessKeyId"] == "access"
