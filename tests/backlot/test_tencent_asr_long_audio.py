"""长音轨分片转写的契约测试。

腾讯云 ASR 单次 Base64 提交上限 5 MB，88.9 分钟的直播回放音轨约 30 MB，
因此必须本地分片、逐片识别、再按片偏移合并。这些测试不联网、不调用 ffmpeg。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from backlot import tencent_asr as t


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("TENCENT_SECRET_ID", "secret-id-never-log")
    monkeypatch.setenv("TENCENT_SECRET_KEY", "secret-key-never-log")


def _chunk_offset(data: bytes) -> float:
    """从假分片字节里读回偏移；非分片数据（短音轨）返回 0。"""
    if not data.startswith(b"FAKECHUNK:"):
        return 0.0
    try:
        return float(data.split(b":", 1)[1].decode("ascii"))
    except (IndexError, ValueError):
        return 0.0


def _fake_tools(monkeypatch, duration: float, record: list | None = None):
    monkeypatch.setattr(t, "_ffmpeg_tools", lambda fallback: ("ffmpeg-fake", "ffprobe-fake"))
    monkeypatch.setattr(t, "probe_audio_seconds", lambda path, ffprobe: duration)
    if record is not None:
        def cut(ffmpeg, source, target, start, span):
            record.append(("cut", start, span))
            # 把分片偏移写进假分片字节：片标识与**提交顺序**无关，并发下结果才可复现。
            target.write_bytes(f"FAKECHUNK:{start:.3f}".encode("ascii"))

        monkeypatch.setattr(t, "_cut_audio_chunk", cut)


def _fake_transcribe(calls: list, seen: list | None = None):
    """假识别器：任务号与文本由分片偏移决定，而非提交顺序。"""

    def transcribe(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        index = int(round(_chunk_offset(data) / t.CHUNK_SECONDS))
        calls.append(len(data))
        if seen is not None:
            seen.append(duration_seconds)
        return (
            f"第{index + 1}片文本",
            [{"start": 5.0, "end": 9.0, "text": f"第{index + 1}片"}],
            {"provider": "tencent-asr-rec-task", "task_id": f"task-{index + 1}"},
        )
    return transcribe


def test_short_audio_still_uses_one_inline_submit(tmp_path, monkeypatch, credentials):
    """60 秒以内必须保持原来的单次提交路径，不引入分片开销。"""
    audio = tmp_path / "short.mp3"
    audio.write_bytes(b"\0" * 2048)
    _fake_tools(monkeypatch, duration=12.5)
    calls, seen = [], []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(calls, seen))
    monkeypatch.setattr(t, "_cut_audio_chunk", lambda *a: pytest.fail("短音轨不应分片"))

    text, segments, metadata = t.transcribe_file(audio)

    assert calls == [2048]
    assert seen == [12.5], "时长必须传给路由逻辑，否则 60 秒以上的音轨会被送进一句话识别"
    assert text == "第1片文本"
    assert metadata["provider"] == "tencent-asr-rec-task"
    assert not (tmp_path / "short.chunks").exists()


def test_over_sixty_second_audio_carries_its_duration_so_it_never_hits_sentence_recognition(tmp_path, monkeypatch, credentials):
    """60 秒以上但体积仍小于 5 MB 时，只按体积分流会错送一句话识别并被腾讯拒收。"""
    audio = tmp_path / "ninety.mp3"
    audio.write_bytes(b"\0" * 4096)
    _fake_tools(monkeypatch, duration=90.0)
    seen = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe([], seen))
    monkeypatch.setattr(t, "_cut_audio_chunk", lambda *a: pytest.fail("体积未超限不应分片"))

    t.transcribe_file(audio)

    assert seen == [90.0]


def test_long_audio_splits_by_offset_and_merges_absolute_timestamps(tmp_path, monkeypatch, credentials):
    """30 分钟音轨应切成 3 片，并把片内时间戳加回绝对偏移。"""
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    cuts: list = []
    _fake_tools(monkeypatch, duration=1800.0, record=cuts)
    calls = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(calls))
    accepted: list = []

    text, segments, metadata = t.transcribe_file(audio, on_accepted=accepted.append)

    assert t.CHUNK_SECONDS == 600.0
    # 分片是并发切的，切片顺序不保证；契约是「切了哪几片」而不是「按什么顺序切」。
    assert sorted(row[1] for row in cuts) == [0.0, 600.0, 1200.0]
    assert sorted(row[2] for row in cuts) == [600.0, 600.0, 600.0]
    assert [row["start"] for row in segments] == [5.0, 605.0, 1205.0]
    assert [row["end"] for row in segments] == [9.0, 609.0, 1209.0]
    assert text == "第1片文本\n第2片文本\n第3片文本"
    assert metadata["provider"] == "tencent-asr-chunked"
    assert metadata["chunk_count"] == 3
    assert metadata["task_ids"] == ["task-1", "task-2", "task-3"]
    assert accepted == ["task-1", "task-2", "task-3"]


def test_successful_chunks_are_cached_so_a_rerun_never_pays_twice(tmp_path, monkeypatch, credentials):
    """中途失败后重跑，已成功的分片必须命中缓存、不再提交。"""
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    _fake_tools(monkeypatch, duration=1200.0, record=[])
    calls = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(calls))

    t.transcribe_file(audio)
    assert len(calls) == 2
    cached = sorted((tmp_path / "long.chunks").glob("chunk-*.json"))
    assert [path.name for path in cached] == ["chunk-00000.json", "chunk-00001.json"]
    assert json.loads(cached[0].read_text(encoding="utf-8"))["offset"] == 0.0

    accepted: list = []
    text, segments, metadata = t.transcribe_file(audio, on_accepted=accepted.append)

    assert len(calls) == 2, "重跑不得再次提交已成功的分片"
    assert metadata["task_ids"] == ["task-1", "task-2"]
    assert accepted == ["task-1", "task-2"]
    assert [row["start"] for row in segments] == [5.0, 605.0]


def test_missing_ffmpeg_pair_refuses_instead_of_silently_truncating(tmp_path, monkeypatch, credentials):
    """定位不到 ffmpeg/ffprobe 时必须报错，不能把长音轨当成短音轨提交。"""
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    monkeypatch.setattr(t, "_ffmpeg_tools", lambda fallback: (None, None))
    monkeypatch.setattr(t, "transcribe_audio_bytes", lambda *a, **k: pytest.fail("不应提交"))

    with pytest.raises(t.TencentASRError, match="FFmpeg"):
        t.transcribe_file(audio)


def test_unreadable_duration_refuses(tmp_path, monkeypatch, credentials):
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    _fake_tools(monkeypatch, duration=0.0)

    with pytest.raises(t.TencentASRError, match="时长"):
        t.transcribe_file(audio)


@pytest.mark.parametrize("nested", [True, False])
def test_rec_task_reads_the_task_id_tencent_actually_returns(monkeypatch, credentials, nested):
    """腾讯把任务号放在 Response.Data.TaskId；此前只读顶层，长音轨一律提交失败。"""
    calls = []

    def fake_request(service, action, payload, **kwargs):
        calls.append(action)
        if action == "CreateRecTask":
            if nested:
                return {"Response": {"RequestId": "r-1", "Data": {"TaskId": 16725759917}}}
            return {"Response": {"RequestId": "r-1", "TaskId": 16725759917}}
        return {"Response": {"Data": {
            "Status": "2", "StatusStr": "success", "ErrorMsg": "",
            "Result": "刚刚开播，家人们赶紧点进来。",
            "ResultDetail": [{"FinalSentence": "刚刚开播，家人们赶紧点进来。", "StartMs": 940, "EndMs": 4240}],
        }}}

    monkeypatch.setattr(t, "tc3_request", fake_request)
    monkeypatch.setattr(t.time, "sleep", lambda _seconds: None)

    # 4 秒以上的音轨会走录音文件识别路由
    text, segments, metadata = t.transcribe_audio_bytes(
        b"\0" * 4096, audio_format="mp3", duration_seconds=600.0, poll_interval_seconds=0.01,
    )

    assert calls == ["CreateRecTask", "DescribeTaskStatus"]
    assert metadata["task_id"] == "16725759917"
    assert segments == [{"start": 0.94, "end": 4.24, "text": "刚刚开播，家人们赶紧点进来。"}]
    assert text == "刚刚开播，家人们赶紧点进来。"


def test_rec_task_refuses_without_a_task_id(monkeypatch, credentials):
    monkeypatch.setattr(t, "tc3_request", lambda *a, **k: {"Response": {"RequestId": "r-2"}})

    with pytest.raises(t.TencentASRError, match="任务号"):
        t.transcribe_audio_bytes(b"\0" * 4096, audio_format="mp3", duration_seconds=600.0)


def test_segment_timestamps_are_clamped_inside_the_submitted_audio():
    """"腾讯末段 EndMs 会略超音频末尾，下游互动索引对越界时间戳是硬拒绝的。"""
    segments = [{"start": 5273.66, "end": 5333.1, "text": "拜拜大家"}]

    assert t._clamp_segments(segments, 5333.035) == [
        {"start": 5273.66, "end": 5333.035, "text": "拜拜大家"},
    ]
    # 未提供时长时保持原样，不猜测
    assert t._clamp_segments(segments, None) == segments
    # 超出上界的 start 也会被收回，并保证 end >= start
    assert t._clamp_segments([{"start": 900.0, "end": 900.0, "text": "x"}], 600.0) == [
        {"start": 600.0, "end": 600.0, "text": "x"},
    ]


# --- P0-2 分片并发：在飞上限、提交次数、保序合并、限流退避、一键回串行 ---


def _concurrency_probe(monkeypatch, *, fail_index: int | None = None,
                       rate_limit_index: int | None = None,
                       ambiguous_index: int | None = None):
    """返回 (在飞峰值读取器, 提交计数读取器)；每次提交 sleep 制造真实重叠窗口。"""
    state = {"live": 0, "peak": 0, "submits": 0, "attempts": {}}
    lock = threading.Lock()

    def transcribe(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        index = int(round(_chunk_offset(data) / t.CHUNK_SECONDS))
        with lock:
            state["live"] += 1
            state["submits"] += 1
            state["peak"] = max(state["peak"], state["live"])
            attempt = state["attempts"].get(index, 0) + 1
            state["attempts"][index] = attempt
        try:
            time.sleep(0.02)
            if rate_limit_index is not None and index == rate_limit_index and attempt == 1:
                raise t.TencentASRError("腾讯云 RequestLimitExceeded：请求过于频繁，请稍后重试")
            if ambiguous_index is not None and index == ambiguous_index:
                # 提交已发出，但连接在返回前中断：受理状态不明确（可能已计费）。
                raise t.TencentASRError("网络连接在返回前中断，是否计费尚不明确")
            if fail_index is not None and index == fail_index:
                raise t.TencentASRError("语音识别服务不可用，请稍后重试")
            return (f"第{index + 1}片文本", [{"start": 5.0, "end": 9.0, "text": f"第{index + 1}片"}],
                    {"provider": "tencent-asr-rec-task", "task_id": f"task-{index + 1}"})
        finally:
            with lock:
                state["live"] -= 1

    monkeypatch.setattr(t, "transcribe_audio_bytes", transcribe)
    return state


def _long_audio(tmp_path, monkeypatch, *, chunks: int):
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    _fake_tools(monkeypatch, duration=t.CHUNK_SECONDS * chunks, record=[])
    return audio


def test_chunks_are_submitted_concurrently_within_the_cap(tmp_path, monkeypatch, credentials):
    """并发上限 3 时必须真的并发提交，且在飞峰值不超过上限、提交次数等于分片数。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=5)
    state = _concurrency_probe(monkeypatch)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "3")

    text, segments, metadata = t.transcribe_file(audio)

    assert metadata["chunk_count"] == 5
    assert state["submits"] == 5, "提交次数必须正好等于分片数（零重复、零遗漏）"
    assert state["peak"] <= 3
    assert state["peak"] >= 2, "并发上限 3 却完全串行提交，说明提速没有生效"
    assert [row["start"] for row in segments] == [5.0, 605.0, 1205.0, 1805.0, 2405.0]
    assert text == "\n".join(f"第{i + 1}片文本" for i in range(5))


def test_merge_is_byte_identical_whatever_the_completion_order(tmp_path, monkeypatch, credentials):
    """后提交的先返回时，文本/任务号/时间戳仍必须按分片序拼装（结果可复现）。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=4)
    state = _concurrency_probe(monkeypatch)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "4")

    text, segments, metadata = t.transcribe_file(audio)

    assert sorted(state["attempts"]) == [0, 1, 2, 3]
    assert text == "第1片文本\n第2片文本\n第3片文本\n第4片文本"
    assert metadata["task_ids"] == ["task-1", "task-2", "task-3", "task-4"]
    assert [row["start"] for row in segments] == [5.0, 605.0, 1205.0, 1805.0]


def test_rate_limited_chunk_is_retried_and_the_others_are_kept(tmp_path, monkeypatch, credentials):
    """限流是被拒的、不计费，可退避重试；其余分片的付费结果不受影响。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    state = _concurrency_probe(monkeypatch, rate_limit_index=1)
    monkeypatch.setattr(t.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "2")

    text, segments, metadata = t.transcribe_file(audio)

    assert state["attempts"] == {0: 1, 1: 2, 2: 1}
    assert metadata["task_ids"] == ["task-1", "task-2", "task-3"]
    assert (tmp_path / "long.chunks" / "chunk-00001.json").is_file()


def test_one_click_serial_kill_switch_bounds_asr_to_one(tmp_path, monkeypatch, credentials):
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    state = _concurrency_probe(monkeypatch)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "4")
    monkeypatch.setenv("HAIKE_FORCE_SERIAL", "1")

    t.transcribe_file(audio)

    assert state["peak"] == 1, "一键回串行必须把在飞压到 1"


def test_failed_chunk_keeps_the_successful_chunks_cached_for_resume(tmp_path, monkeypatch, credentials):
    """一片失败不能丢掉其他片已付费的结果；续跑只重试失败的那一片。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    state = _concurrency_probe(monkeypatch, fail_index=2)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "3")

    with pytest.raises(t.TencentASRError):
        t.transcribe_file(audio)

    cached = sorted(path.name for path in (tmp_path / "long.chunks").glob("chunk-*.json"))
    assert cached == ["chunk-00000.json", "chunk-00001.json"]

    retry_calls: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(retry_calls))
    text, segments, metadata = t.transcribe_file(audio)

    assert len(retry_calls) == 1, "续跑只应重新提交失败的第 3 片"
    assert metadata["task_ids"] == ["task-1", "task-2", "task-3"]
    assert [row["start"] for row in segments] == [5.0, 605.0, 1205.0]


def test_explicit_serial_concurrency_matches_the_single_submit_path(tmp_path, monkeypatch, credentials):
    """显式 concurrency=1 与默认串行逐字节一致（保 N7/现状）。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    calls: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(calls))

    text, segments, metadata = t.transcribe_file(audio, concurrency=1)

    assert len(calls) == 3
    assert text == "第1片文本\n第2片文本\n第3片文本"
    assert metadata["task_ids"] == ["task-1", "task-2", "task-3"]
    assert [row["start"] for row in segments] == [5.0, 605.0, 1205.0]


def test_ambiguous_chunk_submission_is_frozen_and_never_resubmitted(tmp_path, monkeypatch, credentials):
    """受理不明的分片：落 submitting 标记、任务 ambiguous、重跑绝不自动重提。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    _concurrency_probe(monkeypatch, ambiguous_index=1)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "3")

    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)
    assert getattr(raised.value, "status", "") == "ambiguous"
    assert getattr(raised.value, "retryable", True) is False

    manifests = {path.name: json.loads(path.read_text(encoding="utf-8"))
                 for path in (tmp_path / "long.chunks").glob("chunk-*.json")}
    # 受理不明的第 2 片保留 submitting 标记；其余分片是已付费的 done，必须保留。
    assert manifests["chunk-00001.json"]["state"] == t.CHUNK_STATE_SUBMITTING
    assert manifests["chunk-00000.json"]["state"] == t.CHUNK_STATE_DONE
    assert manifests["chunk-00002.json"]["state"] == t.CHUNK_STATE_DONE

    # 重跑：不得重提受理不明的分片，也不得重提任何已完成的分片（零新增付费）。
    replay = _concurrency_probe(monkeypatch)
    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)
    assert replay["submits"] == 0, "受理不明的分片绝不能被自动重提"


def test_ambiguous_chunk_uses_the_callers_resume_hint(tmp_path, monkeypatch, credentials):
    """调用方给了 hint 时，冻结提示把它当**人工核对线索**带上；不得暗示系统已对账，且仍不重提。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    _concurrency_probe(monkeypatch, ambiguous_index=1)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "2")

    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)

    replay = _concurrency_probe(monkeypatch)
    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio, resume_request_id="req-9")
    message = str(raised.value)
    assert "续查标识「req-9」" in message
    assert "人工核对" in message
    # 受理不明的分片没有可查询的服务端 id，文案必须澄清系统并未替用户对账。
    assert "系统未做服务端对账" in message
    assert "已对账" not in message
    assert replay["submits"] == 0


def test_ambiguous_freeze_copy_never_claims_a_charge_or_a_reconciliation(tmp_path, monkeypatch, credentials):
    """首次发现受理不明（未给 hint）的文案：可能已计费 + 不自动重提 + 人工核对，无「已对账」。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=2)
    _concurrency_probe(monkeypatch, ambiguous_index=0)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "1")

    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)
    message = str(raised.value)
    assert "可能已受理并计费" in message
    assert "不会自动重提" in message
    assert "人工核对" in message
    assert "续查标识" not in message
    assert "已对账" not in message


def test_ambiguous_wins_over_a_definite_failure_on_the_first_run(tmp_path, monkeypatch, credentials):
    """同批「低序确定失败 + 高序受理不明」必须收口成 ambiguous，不能被判成可重试的 failed。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=3)
    _concurrency_probe(monkeypatch, fail_index=0, ambiguous_index=2)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "3")

    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)
    assert getattr(raised.value, "status", "") == "ambiguous"
    assert getattr(raised.value, "retryable", True) is False

    cache = tmp_path / "long.chunks"
    names = sorted(path.name for path in cache.glob("chunk-*.json"))
    # 确定失败的第 1 片标记已清除；已完成的第 2 片与受理不明的第 3 片必须保留。
    assert names == ["chunk-00001.json", "chunk-00002.json"]
    assert json.loads((cache / "chunk-00002.json").read_text(encoding="utf-8"))["state"] == t.CHUNK_STATE_SUBMITTING


def test_definite_failure_clears_the_marker_so_resume_may_resubmit(tmp_path, monkeypatch, credentials):
    """确定失败（拿到明确拒绝）不是受理不明：标记必须清除，续跑可以重提该片。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=2)
    _concurrency_probe(monkeypatch, fail_index=1)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "2")

    with pytest.raises(t.TencentASRError) as raised:
        t.transcribe_file(audio)
    assert not isinstance(raised.value, t.TencentASRAmbiguous)

    remaining = sorted(path.name for path in (tmp_path / "long.chunks").glob("chunk-*.json"))
    assert remaining == ["chunk-00000.json"], "确定失败必须清除 submitting 标记"

    calls: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(calls))
    text, segments, metadata = t.transcribe_file(audio)
    assert len(calls) == 1, "确定失败的分片可以在续跑时重提"
    assert metadata["task_ids"] == ["task-1", "task-2"]


# --- 写前日志原子写：崩溃窗口里「要么完整旧内容、要么完整新内容」，永不截断 ---


def test_the_chunk_marker_is_replaced_atomically_from_a_temp_file(tmp_path, monkeypatch, credentials):
    """标记必须走「临时文件 → ``os.replace``」：替换前目标文件始终是完整旧内容。"""
    manifest = tmp_path / "chunk-00000.json"
    manifest.write_text(json.dumps({"offset": 0.0, "state": t.CHUNK_STATE_SUBMITTING}),
                        encoding="utf-8")
    old_text = manifest.read_text(encoding="utf-8")

    observed: dict = {}
    real_replace = t.os.replace

    def spy_replace(src, dst):
        observed["src"] = Path(src)
        observed["dst"] = Path(dst)
        # 替换发生的那一刻，目标文件仍是**完整旧内容**——不能是「已截断、未写完」的中间态。
        observed["target_before"] = Path(dst).read_text(encoding="utf-8")
        # 新内容已经在临时文件里完整落盘，替换只是改个名字。
        observed["staged"] = json.loads(Path(src).read_text(encoding="utf-8"))
        return real_replace(src, dst)

    monkeypatch.setattr(t.os, "replace", spy_replace)
    t._write_chunk_marker(manifest, {"offset": 0.0, "state": t.CHUNK_STATE_DONE, "text": "第1片"})

    assert observed["dst"] == manifest
    assert observed["src"] != manifest
    assert observed["src"].suffix == ".tmp", "必须先写临时文件"
    assert observed["target_before"] == old_text, "替换前目标文件必须是完整旧内容，永不出现截断"
    assert observed["staged"]["state"] == t.CHUNK_STATE_DONE, "新内容必须先在临时文件里完整写好"
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == t.CHUNK_STATE_DONE
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == [], "不得残留临时文件"


def test_a_crash_inside_the_done_write_never_resubmits_the_paid_chunk(tmp_path, monkeypatch, credentials):
    """崩在 done 写入过程中：磁盘上留下的仍是完整的 ``submitting`` 记录 → 重跑冻结、零新增付费。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=1)
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "1")
    submits: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(submits))

    manifest = tmp_path / "long.chunks" / "chunk-00000.json"
    real_replace = t.os.replace
    replacements: list = []

    def crashing_replace(src, dst):
        if Path(dst) == manifest:
            replacements.append(1)
            if len(replacements) == 2:  # 第 1 次是 submitting 写前日志，第 2 次是 done
                raise OSError("模拟：done 写入过程中进程崩溃")
        return real_replace(src, dst)

    monkeypatch.setattr(t.os, "replace", crashing_replace)
    with pytest.raises(OSError):
        t.transcribe_file(audio)

    assert len(submits) == 1, "该分片确实已付费提交过"
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == t.CHUNK_STATE_SUBMITTING, \
        "崩溃窗口留下的必须是完整的旧内容，而不是截断文件"
    assert [p.name for p in manifest.parent.iterdir() if p.suffix == ".tmp"] == []

    # 重跑：绝不重提（零新增付费）。
    monkeypatch.setattr(t.os, "replace", real_replace)
    replay: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(replay))
    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)
    assert replay == [], "崩溃窗口内重跑绝不能重提已付费分片"


def test_a_truncated_manifest_left_by_an_old_writer_freezes_instead_of_resubmitting(
        tmp_path, monkeypatch, credentials):
    """升级路径：旧版原地写崩在中途留下的截断残片，重跑必须冻结，绝不重提（重复付费）。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=1)
    cache = tmp_path / "long.chunks"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "chunk-00000.json").write_text("", encoding="utf-8")  # 已截断、未写完

    submits: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(submits))
    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)

    assert getattr(raised.value, "status", "") == "ambiguous"
    assert submits == [], "截断的残片绝不能被当成「无记录」而重提"


def test_a_readable_manifest_for_another_offset_still_resubmits(tmp_path, monkeypatch, credentials):
    """可读但偏移属于另一套分片的记录**不是**残片：分片边界变了，可安全重提，不得误冻结。"""
    audio = _long_audio(tmp_path, monkeypatch, chunks=1)
    cache = tmp_path / "long.chunks"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "chunk-00000.json").write_text(
        json.dumps({"offset": 600.0, "state": t.CHUNK_STATE_DONE, "text": "旧偏移"}),
        encoding="utf-8")

    submits: list = []
    monkeypatch.setattr(t, "transcribe_audio_bytes", _fake_transcribe(submits))
    text, segments, metadata = t.transcribe_file(audio)

    assert len(submits) == 1, "偏移不匹配的旧记录不得冻结任务（它不是残片，可安全重提）"
    assert metadata["task_ids"] == ["task-1"]


def test_invalid_asr_concurrency_is_reported_in_chinese(tmp_path, monkeypatch, credentials):
    audio = _long_audio(tmp_path, monkeypatch, chunks=2)
    monkeypatch.setattr(t, "transcribe_audio_bytes", lambda *a, **k: pytest.fail("不应提交"))
    monkeypatch.setenv("HAIKE_ASR_CONCURRENCY", "many")

    with pytest.raises(t.TencentASRError, match="并发上限"):
        t.transcribe_file(audio)
