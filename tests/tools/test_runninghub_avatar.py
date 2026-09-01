from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.avatar.runninghub_avatar import (
    AUDIO_FIELD,
    AUDIO_NODE_ID,
    CONTINUATION_COUNT_EXPRESSION,
    CONTINUATION_COUNT_NODE_ID,
    DURATION_SECONDS_EXPRESSION,
    DURATION_SECONDS_NODE_ID,
    INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
    OUTPUT_NODE_ID,
    PRESENTER_FIELD,
    PRESENTER_NODE_ID,
    PREVIEW_OUTPUT_NODE_ID,
    REMOTE_CONTINUATION_COUNT_EXPRESSION,
    RunningHubAvatarError,
    RunningHubLongCatClient,
    _validate_infinitetalk_448x560_long_template,
    _validate_infinitetalk_448x560_exact_clock_template,
    billing_evidence,
    longcat_duration_plan,
    load_longcat_workflow_template,
    repair_longcat_workflow,
)


class Response:
    def __init__(self, payload, *, ok=True, status_code=200, chunks=None):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024):
        yield from self._chunks


def test_frozen_workflow_has_only_expected_mutable_inputs():
    path = Path("config/runninghub/longcat_avatar_api.json")
    workflow = load_longcat_workflow_template(path)
    assert workflow[PRESENTER_NODE_ID]["class_type"] == "LoadImage"
    assert PRESENTER_FIELD in workflow[PRESENTER_NODE_ID]["inputs"]
    assert workflow[AUDIO_NODE_ID]["class_type"] == "LoadAudio"
    assert AUDIO_FIELD in workflow[AUDIO_NODE_ID]["inputs"]
    assert workflow[OUTPUT_NODE_ID]["class_type"] == "VHS_VideoCombine"
    assert workflow[DURATION_SECONDS_NODE_ID]["inputs"]["expression"] == DURATION_SECONDS_EXPRESSION
    assert workflow[CONTINUATION_COUNT_NODE_ID]["inputs"]["expression"] == CONTINUATION_COUNT_EXPRESSION
    assert workflow[CONTINUATION_COUNT_NODE_ID]["inputs"]["c"] == ["148", 0]
    assert workflow[PREVIEW_OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] is True
    assert workflow[OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] is True
    assert len(workflow) == 55


def test_node_override_contract_never_changes_model_nodes():
    overrides = RunningHubLongCatClient.node_info_list(
        presenter_filename="presenter.png",
        audio_filename="speech.wav",
    )
    assert overrides == [
        {"nodeId": "176", "fieldName": "image", "fieldValue": "presenter.png"},
        {"nodeId": "524", "fieldName": "audio", "fieldValue": "speech.wav"},
        {"nodeId": "529", "fieldName": "expression", "fieldValue": DURATION_SECONDS_EXPRESSION},
        {"nodeId": "546", "fieldName": "expression", "fieldValue": REMOTE_CONTINUATION_COUNT_EXPRESSION},
        {"nodeId": "292", "fieldName": "trim_to_audio", "fieldValue": True},
        {"nodeId": "352", "fieldName": "trim_to_audio", "fieldValue": True},
    ]


@pytest.mark.parametrize(
    ("duration", "aligned_frames", "continuations", "generated_frames"),
    [
        (2.0, 53, 0, 101),
        (5.0, 129, 1, 189),
        (12.0, 301, 3, 365),
        (20.0, 501, 5, 541),
    ],
)
def test_duration_plan_uses_seconds_covers_audio_and_trims_exactly(
    duration: float,
    aligned_frames: int,
    continuations: int,
    generated_frames: int,
):
    plan = longcat_duration_plan(duration)

    assert plan["raw_frames"] == duration * 25
    assert plan["aligned_frames"] == aligned_frames
    assert plan["continuation_count"] == continuations
    assert plan["generated_frames_before_trim"] == generated_frames
    assert plan["generated_frames_before_trim"] >= plan["aligned_frames"]
    assert plan["expected_output_duration"] == duration


def test_repair_upgrades_the_exported_buggy_duration_graph():
    workflow = load_longcat_workflow_template(Path("config/runninghub/longcat_avatar_api.json"))
    broken = json.loads(json.dumps(workflow))
    broken[DURATION_SECONDS_NODE_ID]["inputs"]["expression"] = "a"
    broken[CONTINUATION_COUNT_NODE_ID]["inputs"]["expression"] = "ceil(a/b)-1"
    broken[CONTINUATION_COUNT_NODE_ID]["inputs"].pop("c")
    broken[PREVIEW_OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] = False
    broken[OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] = False

    repaired = repair_longcat_workflow(broken)

    assert repaired[DURATION_SECONDS_NODE_ID]["inputs"]["expression"] == DURATION_SECONDS_EXPRESSION
    assert repaired[CONTINUATION_COUNT_NODE_ID]["inputs"]["expression"] == CONTINUATION_COUNT_EXPRESSION
    assert repaired[CONTINUATION_COUNT_NODE_ID]["inputs"]["c"] == ["148", 0]
    assert repaired[PREVIEW_OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] is True
    assert repaired[OUTPUT_NODE_ID]["inputs"]["trim_to_audio"] is True


def test_unrepaired_millisecond_graph_is_rejected_before_paid_use(tmp_path: Path):
    workflow = load_longcat_workflow_template(Path("config/runninghub/longcat_avatar_api.json"))
    workflow[DURATION_SECONDS_NODE_ID]["inputs"]["expression"] = "a"
    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(json.dumps(workflow), encoding="utf-8")

    with pytest.raises(RunningHubAvatarError, match="除以 1000"):
        load_longcat_workflow_template(unsafe)


def test_upload_submit_poll_and_download_without_real_network(tmp_path: Path):
    session = Mock()
    session.post.side_effect = [
        Response({"code": 0, "data": {"fileName": "remote-presenter.png"}}),
        Response({"code": 0, "data": {"fileName": "remote-audio.wav"}}),
        Response({"code": 0, "data": {"taskId": "task-001"}}),
        Response({
            "taskId": "task-001",
            "status": "SUCCESS",
            "results": [{"outputType": "mp4", "url": "https://files.example/result.mp4", "consumeCoins": 12}],
        }),
    ]
    session.get.return_value = Response({}, chunks=[b"video-bytes"])
    client = RunningHubLongCatClient(api_key="test-key", workflow_id="123456", session=session)
    image = tmp_path / "person.png"
    audio = tmp_path / "speech.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")

    image_name = client.upload_file(image, file_type="image")
    audio_name = client.upload_file(audio, file_type="audio")
    submitted = client.submit(presenter_filename=image_name, audio_filename=audio_name)
    result = client.poll(submitted["task_id"])
    target = tmp_path / "result.mp4"
    client.download(result["video_url"], target)

    assert submitted["task_id"] == "task-001"
    assert result["status"] == "SUCCEEDED"
    assert result["consume_coins"] == 12
    assert target.read_bytes() == b"video-bytes"
    assert session.post.call_args_list[0].args[0].endswith("/openapi/v2/media/upload/binary")
    assert session.post.call_args_list[1].args[0].endswith("/openapi/v2/media/upload/binary")
    submit_body = session.post.call_args_list[2].kwargs["json"]
    assert submit_body["workflowId"] == "123456"
    assert submit_body["nodeInfoList"] == RunningHubLongCatClient.node_info_list(
        presenter_filename="remote-presenter.png",
        audio_filename="remote-audio.wav",
    )
    query_call = session.post.call_args_list[3]
    assert query_call.args[0].endswith("/openapi/v2/query")
    assert query_call.kwargs["json"] == {"taskId": "task-001"}
    assert "apiKey" not in query_call.kwargs["json"]


@pytest.mark.parametrize(
    "workflow_profile",
    ["infinitetalk_384x480_short", "infinitetalk_448x560_long"],
)
def test_infinitetalk_profiles_use_shared_image_audio_output_nodes(workflow_profile):
    nodes = RunningHubLongCatClient.node_info_list(
        presenter_filename="person.png",
        audio_filename="speech.wav",
        workflow_profile=workflow_profile,
    )

    assert nodes == [
        {"nodeId": "36", "fieldName": "image", "fieldValue": "person.png"},
        {"nodeId": "34", "fieldName": "audio", "fieldValue": "speech.wav"},
        {"nodeId": "24", "fieldName": "trim_to_audio", "fieldValue": True},
    ]


def test_exact_clock_profile_requires_and_overrides_node_35():
    nodes = RunningHubLongCatClient.node_info_list(
        presenter_filename="person.png",
        audio_filename="speech.wav",
        workflow_profile=INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
        exact_total_frames=118,
    )

    assert nodes == [
        {"nodeId": "36", "fieldName": "image", "fieldValue": "person.png"},
        {"nodeId": "34", "fieldName": "audio", "fieldValue": "speech.wav"},
        {"nodeId": "35", "fieldName": "value", "fieldValue": 118},
        {"nodeId": "24", "fieldName": "trim_to_audio", "fieldValue": True},
    ]


def test_exact_clock_submit_without_frames_fails_before_http():
    session = Mock()
    client = RunningHubLongCatClient(
        api_key="enterprise-key",
        workflow_id=INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
        workflow_profile=INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
        session=session,
    )

    with pytest.raises(RunningHubAvatarError, match="正整数总帧数"):
        client.submit(
            presenter_filename="person.png",
            audio_filename="speech.wav",
            instance_type="default",
        )

    session.post.assert_not_called()


def test_exact_clock_standard_submit_persists_frame_contract():
    session = Mock()
    session.post.return_value = Response({"code": 0, "data": {"taskId": "task-exact"}})
    client = RunningHubLongCatClient(
        api_key="enterprise-key",
        workflow_id=INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
        workflow_profile=INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
        session=session,
    )

    result = client.submit(
        presenter_filename="person.png",
        audio_filename="speech.wav",
        instance_type="default",
        exact_total_frames=118,
    )

    assert result["request_contract"]["exact_total_frames"] == 118
    assert session.post.call_args.kwargs["json"]["instanceType"] == "default"
    assert {"nodeId": "35", "fieldName": "value", "fieldValue": 118} in session.post.call_args.kwargs["json"]["nodeInfoList"]


def test_frozen_infinitetalk_448x560_long_workflow_contract():
    path = Path("config/runninghub/workflow-2093219950461808641.api.json")

    digest = _validate_infinitetalk_448x560_long_template(path)

    assert len(digest) == 64


def test_deployed_infinitetalk_exact_clock_workflow_contract():
    path = Path("config/runninghub/workflow-2094449979141218305.api.json")

    digest = _validate_infinitetalk_448x560_exact_clock_template(path)

    assert digest == "f901ee9a13645ec75913164b38ca6c6a9d7f3519324854071f98310f397f1562"


def test_poll_v2_keeps_running_task_resumable():
    session = Mock()
    session.post.return_value = Response({
        "taskId": "task-running",
        "status": "RUNNING",
        "results": None,
        "errorCode": "",
        "errorMessage": "",
    })
    client = RunningHubLongCatClient(api_key="test-key", workflow_id="123456", session=session)

    result = client.poll("task-running")

    assert result["status"] == "RUNNING"
    assert result["video_url"] is None


def test_poll_v2_exposes_safe_oom_diagnostics_without_traceback():
    session = Mock()
    session.post.return_value = Response({
        "taskId": "task-oom",
        "status": "FAILED",
        "errorCode": "805",
        "errorMessage": "工作流运行失败",
        "failedReason": {
            "exception_type": "torch.OutOfMemoryError",
            "node_id": "13",
            "node_name": "WanVideoSampler",
            "exception_message": "显存不足",
            "traceback": "sensitive provider traceback must not persist",
            "current_inputs": {"signed_url": "https://example.invalid/secret"},
        },
        "usage": {"consumeMoney": "0.0471", "taskCostTime": "42.39"},
    })
    client = RunningHubLongCatClient(api_key="enterprise-key", workflow_id="123456", session=session)

    result = client.poll("task-oom")

    assert result["status"] == "FAILED"
    assert "torch.OutOfMemoryError" in result["error"]
    assert result["failure_details"] == {
        "error_code": "805",
        "error_message": "工作流运行失败",
        "exception_type": "torch.OutOfMemoryError",
        "node_id": "13",
        "node_name": "WanVideoSampler",
        "exception_message": "显存不足",
    }
    assert "traceback" not in json.dumps(result["failure_details"], ensure_ascii=False)
    assert "signed_url" not in json.dumps(result["failure_details"], ensure_ascii=False)
    assert result["billing"]["observed_instance"] == "standard_24gb"


def test_enterprise_poll_exposes_monetary_cost_for_budget_ledger():
    session = Mock()
    session.post.return_value = Response({
        "taskId": "task-enterprise-cost",
        "status": "SUCCESS",
        "consumeMoney": 0.37,
        "results": [{"outputType": "mp4", "url": "https://files.example/result.mp4"}],
    })
    client = RunningHubLongCatClient(api_key="enterprise-key", workflow_id="123456", session=session)

    result = client.poll("task-enterprise-cost")

    assert result["status"] == "SUCCEEDED"
    assert result["consume_money_cny"] == pytest.approx(0.37)


def test_provider_usage_rate_is_auditable_without_guessing_instance_type():
    evidence = billing_evidence({
        "usage": {"consumeMoney": "2.871", "taskCostTime": "2584", "consumeCoins": None},
    })

    assert evidence["provider_usage"]["consume_money"] == pytest.approx(2.871)
    assert evidence["provider_usage"]["task_cost_seconds"] == pytest.approx(2584)
    assert evidence["observed_hourly_rate_cny"] == pytest.approx(4.0, abs=0.001)
    assert evidence["observed_instance"] == "standard_24gb"


def test_enterprise_standard_submit_sets_explicit_default_instance():
    session = Mock()
    session.post.return_value = Response({"code": 0, "data": {"taskId": "task-enterprise"}})
    client = RunningHubLongCatClient(api_key="enterprise-key", workflow_id="123456", session=session)

    result = client.submit(
        presenter_filename="presenter.png",
        audio_filename="speech.wav",
        instance_type="default",
    )

    assert result["task_id"] == "task-enterprise"
    assert session.post.call_args.kwargs["json"]["instanceType"] == "default"


def test_enterprise_lite_submit_omits_instance_type():
    session = Mock()
    session.post.return_value = Response({"code": 0, "data": {"taskId": "task-lite"}})
    client = RunningHubLongCatClient(api_key="enterprise-key", workflow_id="123456", session=session)

    result = client.submit(
        presenter_filename="presenter.png",
        audio_filename="speech.wav",
        instance_type=None,
    )

    assert result["task_id"] == "task-lite"
    assert "instanceType" not in session.post.call_args.kwargs["json"]
    assert result["request_contract"]["instance_type_present"] is False
    assert result["request_contract"]["instance_type_value"] is None
    assert "instanceType" not in result["request_contract"]["keys"]


def test_literal_lite_instance_type_is_rejected_before_provider_call():
    session = Mock()
    client = RunningHubLongCatClient(api_key="enterprise-key", workflow_id="123456", session=session)

    with pytest.raises(RunningHubAvatarError, match="Lite 必须省略"):
        client.submit(
            presenter_filename="presenter.png",
            audio_filename="speech.wav",
            instance_type="lite",
        )

    session.post.assert_not_called()


def test_poll_v2_reports_success_without_mp4_as_provider_failure():
    session = Mock()
    session.post.return_value = Response({
        "taskId": "task-image-only",
        "status": "SUCCESS",
        "results": [{"outputType": "png", "url": "https://files.example/result.png"}],
    })
    client = RunningHubLongCatClient(api_key="test-key", workflow_id="123456", session=session)

    result = client.poll("task-image-only")

    assert result["status"] == "FAILED"
    assert "MP4" in result["error"]


def test_missing_workflow_id_blocks_before_paid_submission():
    with pytest.raises(RunningHubAvatarError, match="WORKFLOW_ID"):
        RunningHubLongCatClient(api_key="test-key", workflow_id="")


def test_provider_error_does_not_echo_api_key():
    session = Mock()
    session.post.return_value = Response({"code": 500, "msg": "bad request"}, ok=True)
    client = RunningHubLongCatClient(api_key="secret-key", workflow_id="123456", session=session)
    with pytest.raises(RunningHubAvatarError, match="bad request") as error:
        client.submit(presenter_filename="a.png", audio_filename="a.wav")
    assert "secret-key" not in str(error.value)
