"""Round 3 QA — adversarial re-verification of the Tencent-ASR write-ahead journal.

Every case here is constructed by QA (not the implementer's tests). Covers the
seven points the lead asked for:

1. timing      — the ``submitting`` marker must be on disk *inside* the paid submit;
2. crash       — a real subprocess hard-kill mid-submit must freeze, not re-pay;
3. three-state — ``_read_chunk_manifest`` truly tells submitting / done / nothing;
4. retriable   — a definite failure clears the marker so a resume may re-submit;
5. resume-hint — ``resume_request_id`` is used on the chunk path, not the single path;
6. workbench   — the tencent freeze guard lands the job ``ambiguous`` (not ``failed``);
7. reverse     — no chunk failure is ever swallowed and treated as success.

No network, no real paid calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backlot import tencent_asr as t

REPO_ROOT = Path(__file__).resolve().parents[2]
CRASH_WORKER = REPO_ROOT / "temp" / "qa_asr_crash_worker.py"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _offset(data: bytes) -> float:
    if not data.startswith(b"FAKECHUNK:"):
        return 0.0
    try:
        return float(data.split(b":", 1)[1].decode("ascii"))
    except (IndexError, ValueError):
        return 0.0


def _fixtures(monkeypatch, *, chunks: float):
    """Patch ffmpeg/ffprobe/chunk-cut so the long-audio path runs offline."""
    monkeypatch.setattr(t, "REC_TASK_MAX_BYTES", 1024)
    monkeypatch.setattr(t, "_ffmpeg_tools", lambda fallback: ("ffmpeg-fake", "ffprobe-fake"))
    monkeypatch.setattr(t, "probe_audio_seconds", lambda path, ffprobe: t.CHUNK_SECONDS * chunks)

    def cut(ffmpeg, source, target, start, span):
        target.write_bytes(f"FAKECHUNK:{start:.3f}".encode("ascii"))

    monkeypatch.setattr(t, "_cut_audio_chunk", cut)


def _audio(tmp_path) -> Path:
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)  # > patched REC_TASK_MAX_BYTES -> chunked path
    return audio


def _manifests(tmp_path) -> dict:
    cache = tmp_path / "long.chunks"
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(cache.glob("chunk-*.json"))
    }


def _run_bounded_stub(monkeypatch, handler):
    """Install a fake ``transcribe_audio_bytes`` and a submit-index counter."""
    submits: list = []

    def transcribe(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        idx = int(round(_offset(data) / t.CHUNK_SECONDS))
        submits.append(idx)
        return handler(idx)

    monkeypatch.setattr(t, "transcribe_audio_bytes", transcribe)
    return submits


def _ok(idx: int):
    return (
        f"第{idx + 1}片文本",
        [{"start": 5.0, "end": 9.0, "text": f"第{idx + 1}片"}],
        {"provider": "tencent-asr-rec-task", "task_id": f"task-{idx + 1}"},
    )


# --------------------------------------------------------------------------- #
# 1. timing — the marker must precede the paid submit
# --------------------------------------------------------------------------- #
def test_write_ahead_marker_exists_at_the_paid_submit_entry(tmp_path, monkeypatch):
    """Inspect the journal from *inside* the submit: a write-after impl fails here."""
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=1)
    cache_dir = tmp_path / "long.chunks"
    seen: dict = {}

    def transcribe(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        # This IS the instant the paid submit starts.
        manifest = cache_dir / "chunk-00000.json"
        seen["exists"] = manifest.is_file()
        seen["content"] = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
        raise t.TencentASRError("网络连接在返回前中断，是否计费尚不明确")

    monkeypatch.setattr(t, "transcribe_audio_bytes", transcribe)

    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)

    assert seen["exists"] is True, "写前日志缺失：付费提交入口时 marker 还不在盘上（实为写后日志）"
    marker = seen["content"]
    assert marker["state"] == t.CHUNK_STATE_SUBMITTING
    assert marker["offset"] == 0.0
    assert abs(marker["span"] - t.CHUNK_SECONDS) < 1e-3
    assert marker.get("submitted_at")
    assert marker.get("request_hint")


# --------------------------------------------------------------------------- #
# 3. three-state journal
# --------------------------------------------------------------------------- #
def test_manifest_three_state_and_offset_discipline(tmp_path, monkeypatch):
    manifest = tmp_path / "chunk-00000.json"
    assert t._read_chunk_manifest(manifest, 0.0) is None  # no record
    assert t._chunk_state(None) == ""

    manifest.write_text(json.dumps({"offset": 0.0, "state": "submitting"}), encoding="utf-8")
    assert t._chunk_state(t._read_chunk_manifest(manifest, 0.0)) == "submitting"

    manifest.write_text(json.dumps({"offset": 0.0, "state": "done"}), encoding="utf-8")
    assert t._chunk_state(t._read_chunk_manifest(manifest, 0.0)) == "done"

    # a legacy cache entry without a ``state`` key must be treated as done
    manifest.write_text(json.dumps({"offset": 0.0, "text": "旧缓存"}), encoding="utf-8")
    assert t._chunk_state(t._read_chunk_manifest(manifest, 0.0)) == "done"

    # the offset-0 pitfall: 0.0 is falsy, yet it must still match
    manifest.write_text(json.dumps({"offset": 0.0, "state": "done"}), encoding="utf-8")
    assert t._read_chunk_manifest(manifest, 0.0) is not None

    # an offset mismatch must NOT reuse another chunk's text
    manifest.write_text(json.dumps({"offset": 600.0, "state": "done"}), encoding="utf-8")
    assert t._read_chunk_manifest(manifest, 0.0) is None

    for junk in ("{ not json", "[1, 2, 3]", "null", '""'):
        manifest.write_text(junk, encoding="utf-8")
        assert t._read_chunk_manifest(manifest, 0.0) is None, f"损坏记录 {junk!r} 必须按无记录处理"


# --------------------------------------------------------------------------- #
# 4. a definite failure stays retriable
# --------------------------------------------------------------------------- #
def test_definite_failure_clears_marker_and_resume_resubmits(tmp_path, monkeypatch):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=2)

    def definite(idx):
        if idx == 1:
            raise t.TencentASRError("语音识别服务不可用，请稍后重试")  # explicit reject, not billed
        return _ok(idx)

    _run_bounded_stub(monkeypatch, definite)
    with pytest.raises(t.TencentASRError) as raised:
        t.transcribe_file(audio)
    assert not isinstance(raised.value, t.TencentASRAmbiguous)

    assert list(_manifests(tmp_path)) == ["chunk-00000.json"], "确定失败必须清除 submitting 标记"

    submits = _run_bounded_stub(monkeypatch, _ok)
    _text, _segs, metadata = t.transcribe_file(audio)
    assert submits == [1], "已清除标记的确定失败分片必须可以在续跑时重提"
    assert metadata["task_ids"] == ["task-1", "task-2"]


def test_exhausted_rate_limit_is_definite_so_marker_is_cleared(tmp_path, monkeypatch):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=1)
    monkeypatch.setattr(t.time, "sleep", lambda _s: None)
    attempts: list = []

    def limited(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        attempts.append(1)
        raise t.TencentASRError("腾讯云 RequestLimitExceeded：请求过于频繁，请稍后重试")

    monkeypatch.setattr(t, "transcribe_audio_bytes", limited)
    with pytest.raises(t.TencentASRError) as raised:
        t.transcribe_file(audio)

    assert not isinstance(raised.value, t.TencentASRAmbiguous), "被拒的限流不是受理不明"
    assert len(attempts) == t.CHUNK_RETRY_ATTEMPTS, "限流必须退避重试到上限"
    assert not (tmp_path / "long.chunks" / "chunk-00000.json").exists(), "限流耗尽后标记必须清除"


# --------------------------------------------------------------------------- #
# 5. resume_request_id wiring
# --------------------------------------------------------------------------- #
def test_resume_hint_recovers_from_a_prior_ambiguous_chunk(tmp_path, monkeypatch):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=1)

    def ambiguous(_idx):
        raise t.TencentASRError("连接在返回前中断，是否计费尚不明确")

    _run_bounded_stub(monkeypatch, ambiguous)
    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)  # run 1 leaves a ``submitting`` marker

    # run 2 with a reconciliation hint: the hint VALUE must surface, nothing is paid,
    # and the copy must NOT claim the system already reconciled on the operator's behalf.
    submits = _run_bounded_stub(monkeypatch, _ok)
    with pytest.raises(t.TencentASRAmbiguous) as hinted:
        t.transcribe_file(audio, resume_request_id="req-9")
    hinted_message = str(hinted.value)
    assert "req-9" in hinted_message, "提交线索值必须展示给人工核对"
    assert submits == [], "给出提交线索时绝不能重提"
    assert "已对账" not in hinted_message and "对账完成" not in hinted_message, \
        "不得声称系统已替用户完成服务端对账"

    # run 3 without a hint: still refuses, with generic actionable Chinese guidance
    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio, resume_request_id=None)
    assert "req-9" not in str(raised.value)
    assert "核对" in str(raised.value)


def test_single_submit_path_never_receives_resume_request_id(tmp_path, monkeypatch):
    audio = tmp_path / "short.mp3"
    audio.write_bytes(b"\0" * 512)  # <= 5 MB -> the single inline submit
    monkeypatch.setattr(t, "_ffmpeg_tools", lambda fallback: ("ffmpeg-fake", "ffprobe-fake"))
    monkeypatch.setattr(t, "probe_audio_seconds", lambda path, ffprobe: 12.0)
    seen: dict = {}

    def one_shot(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module, **extra):
        seen.update(extra)
        seen["_called"] = True
        return ("短音轨文本", [], {"provider": "tencent-asr-sentence", "request_id": "r-1"})

    monkeypatch.setattr(t, "transcribe_audio_bytes", one_shot)
    monkeypatch.setattr(t, "_cut_audio_chunk", lambda *a: pytest.fail("短音轨不应分片"))

    t.transcribe_file(audio, resume_request_id="req-9")

    assert seen.get("_called") is True
    assert "resume_request_id" not in seen, "单次提交路径不得被传入续查标识（接口未变）"


# --------------------------------------------------------------------------- #
# 6. workbench tencent freeze guard
# --------------------------------------------------------------------------- #
def _seed_project(tmp_path, monkeypatch):
    from backlot import material_interactions as m
    from backlot import server as server
    from backlot import state as state_module
    from backlot import workbench as wb

    root = tmp_path / "projects"
    project = root / "qa-round3"
    (project / "assets").mkdir(parents=True)
    m._write_json(project / "project.json", {"project_id": project.name, "title": "QA", "pipeline_type": "cinematic"})
    (project / "assets" / "source.mp4").write_bytes(b"source")
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {
        "name": "原片", "type": "video", "source_type": "human_provided",
        "path": "assets/source.mp4", "duration_seconds": 30,
    })
    wb._save(project, state)

    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    monkeypatch.setattr(state_module, "PROJECTS_DIR", root)
    monkeypatch.setattr(server, "_summary_cache", {})
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _: "ffprobe")
    monkeypatch.setattr("backlot.media_index.probe_media", lambda *_: {"duration_seconds": 30})
    monkeypatch.setattr(m, "runtime_identity", lambda: {"model": "test"})
    monkeypatch.setattr(wb, "default_transcript_provider", lambda: "doubao")
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: None)
    monkeypatch.setattr(wb, "doubao_asr_runtime_identity", lambda: "asr-test")
    return project, asset["id"], wb


def test_tencent_submitting_checkpoint_freezes_job_as_ambiguous(tmp_path, monkeypatch):
    project, asset_id, wb = _seed_project(tmp_path, monkeypatch)
    queued = wb.start_asset_media_index(project, asset_id, {"stage": "overview", "remote_vision_confirmed": True})
    job_id = queued["automation"]["media_index"]["job_id"]

    state = wb.read_workbench(project)
    job = state["automation"]["media_index"]
    job["request"]["transcript_provider"] = "tencent"
    job["request"]["transcribe"] = True
    job["request"]["cloud_asr"] = {"status": "submitting", "engine": "tencent", "request_id": "r-x"}
    wb._save(project, state)

    with pytest.raises(wb.TencentASRAmbiguous) as raised:
        wb.generate_asset_media_index(project, job_id)
    assert raised.value.status == "ambiguous"
    assert raised.value.retryable is False

    wb.mark_asset_media_index_failed(project, job_id, raised.value)
    frozen = wb.read_workbench(project)["automation"]["media_index"]
    assert frozen["status"] == "ambiguous", "受理不明必须冻结为 ambiguous，不能是可重试的 failed"
    assert frozen["error_detail"]["retryable"] is False
    assert frozen["error_detail"]["next_action"].startswith("请先核对供应商")


def test_tencent_accepted_checkpoint_does_not_freeze(tmp_path, monkeypatch):
    """Negative control: a resolved checkpoint must NOT trigger the freeze guard."""
    project, asset_id, wb = _seed_project(tmp_path, monkeypatch)
    queued = wb.start_asset_media_index(project, asset_id, {"stage": "overview", "remote_vision_confirmed": True})
    job_id = queued["automation"]["media_index"]["job_id"]

    state = wb.read_workbench(project)
    job = state["automation"]["media_index"]
    job["request"]["transcript_provider"] = "tencent"
    job["request"]["cloud_asr"] = {"status": "accepted", "engine": "tencent", "request_id": "r-x"}
    wb._save(project, state)

    try:
        wb.generate_asset_media_index(project, job_id)
    except wb.TencentASRAmbiguous as exc:  # pragma: no cover - would be the bug
        pytest.fail(f"已受理的 checkpoint 不应被冻结：{exc}")
    except Exception:
        pass  # downstream may fail in this minimal harness; the guard is what matters


# --------------------------------------------------------------------------- #
# 7. reverse — a failing chunk is never swallowed into success
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [0, 2])
def test_a_single_failing_chunk_always_surfaces(tmp_path, monkeypatch, bad):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=3)

    def handler(idx):
        if idx == bad:
            raise t.TencentASRError("语音识别服务不可用")
        return _ok(idx)

    _run_bounded_stub(monkeypatch, handler)
    with pytest.raises(t.TencentASRError):
        t.transcribe_file(audio)


def test_all_chunks_failing_raises_instead_of_returning(tmp_path, monkeypatch):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=3)

    def handler(_idx):
        raise t.TencentASRError("语音识别服务不可用")

    _run_bounded_stub(monkeypatch, handler)
    with pytest.raises(t.TencentASRError) as raised:
        t.transcribe_file(audio)
    assert not isinstance(raised.value, t.TencentASRAmbiguous)


def test_non_tencent_exception_is_not_swallowed(tmp_path, monkeypatch):
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=3)

    def handler(idx):
        if idx == 1:
            raise RuntimeError("unexpected internal failure")
        return _ok(idx)

    _run_bounded_stub(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        t.transcribe_file(audio)
    # conservative: the marker survives so a rerun freezes rather than re-pays
    assert _manifests(tmp_path)["chunk-00001.json"]["state"] == t.CHUNK_STATE_SUBMITTING


def test_ambiguous_chunk_freezes_even_when_a_sibling_fails_first(tmp_path, monkeypatch):
    """同一批里低序确定失败 + 高序受理不明：必须以 ambiguous 收口，且重跑 0 付费。"""
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=3)

    def handler(idx):
        if idx == 0:
            raise t.TencentASRError("语音识别服务不可用")  # definite, lower index
        if idx == 1:
            raise t.TencentASRError("连接在返回前中断，是否计费尚不明确")  # ambiguous
        return _ok(idx)

    _run_bounded_stub(monkeypatch, handler)
    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)
    assert raised.value.status == "ambiguous", "只要有一个分片受理不明，整条任务就必须 ambiguous 收口"
    assert raised.value.retryable is False

    manifests = _manifests(tmp_path)
    assert manifests["chunk-00001.json"]["state"] == t.CHUNK_STATE_SUBMITTING, "受理不明标记必须保留"

    submits = _run_bounded_stub(monkeypatch, _ok)
    with pytest.raises(t.TencentASRAmbiguous):
        t.transcribe_file(audio)
    assert submits == [], "存在受理不明分片时，重跑绝不能提交任何分片"


# --------------------------------------------------------------------------- #
# 8. atomic done-marker + unreadable-record hardening
# --------------------------------------------------------------------------- #
def test_corrupt_done_record_freezes_instead_of_resubmitting(tmp_path, monkeypatch):
    """文件在盘上却读不出（崩溃残片）→ 按受理不明冻结，绝不盲目重提。"""
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=1)
    cache = tmp_path / "long.chunks"
    cache.mkdir()
    (cache / "chunk-00000.json").write_text("", encoding="utf-8")  # truncated/empty residue

    submits = _run_bounded_stub(monkeypatch, _ok)
    with pytest.raises(t.TencentASRAmbiguous) as raised:
        t.transcribe_file(audio)
    assert raised.value.status == "ambiguous"
    assert submits == [], "不可读残片绝不能被当作无记录重提"


def test_readable_record_for_another_offset_is_not_frozen(tmp_path, monkeypatch):
    """可读、合法的记录、只是 offset 属于另一片 → 不得误冻结，该片应照常提交。"""
    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=2)
    cache = tmp_path / "long.chunks"
    cache.mkdir()
    # 合法 JSON、合法 float offset，但不匹配本片（0.0）——属于「另一套分片」的合法记录。
    (cache / "chunk-00000.json").write_text(
        json.dumps({"offset": 600.0, "span": 600.0, "state": "done", "text": "别的片"}),
        encoding="utf-8",
    )

    submits = _run_bounded_stub(monkeypatch, _ok)
    text, _segs, _meta = t.transcribe_file(audio)  # 必须不冻结
    # 提交是**并发**进行的：引擎只承诺「合并结果按 offset 升序确定性」，
    # **从不**承诺「提交顺序」（见 `_transcribe_long_file` 文档 + `run_bounded`）。
    # 因此这里断言提交**集合**，顺序无关 —— 曾把 `== [0, 1]` 写死，在全量
    # 负载下命中 `[1, 0]` 而误报失败（测试过拟合，非产品缺陷）。
    assert sorted(submits) == [0, 1], "可读的异片记录不得触发冻结，两片都应被提交（顺序无关）"
    assert text == "第1片文本\n第2片文本"


def test_merged_transcript_is_offset_ordered_regardless_of_completion_order(tmp_path, monkeypatch):
    """设计保证：并发完成顺序再乱，合并后的文本 / 任务号 / 分片仍严格按 offset 升序。

    这才是引擎**真正承诺**的性质（``_transcribe_long_file``：「最后严格按分片序
    拼装文本 / 任务号 / 时间戳，因此乱序完成也与串行逐字节一致」）。之前的用例
    误把「提交顺序」当成不变量 —— 本条用 ``threading.Event`` **强制后一片先完成**，
    再验证合并结果不被完成顺序污染。
    """
    import threading

    if t.resolve_limit(t.KIND_ASR, 2) == 1:
        pytest.skip("ASR 串行开关生效，无法构造逆序完成（该性质仅在有界并发下可测）")

    audio = _audio(tmp_path)
    _fixtures(monkeypatch, chunks=2)

    first_chunk_done = threading.Event()
    submits: list[int] = []      # 提交**开始**的顺序（并发下不确定）
    completion: list[int] = []   # 完成顺序（本用例强制为逆序）

    def transcribe(data, *, audio_format, duration_seconds=None, timeout_seconds, requests_module):
        idx = int(round(_offset(data) / t.CHUNK_SECONDS))
        submits.append(idx)
        if idx == 1:
            first_chunk_done.set()          # 让高 offset 的分片先返回
            completion.append(1)
            return _ok(idx)
        # 低 offset 的分片必须等「后一片已完成」才返回 —— 完成顺序被强制为 [1, 0]
        assert first_chunk_done.wait(timeout=10), "并发未生效（限流=1？）：无法构造乱序完成"
        completion.append(0)
        return _ok(idx)

    monkeypatch.setattr(t, "transcribe_audio_bytes", transcribe)

    text, segments, metadata = t.transcribe_file(audio, concurrency=2)

    # 完成顺序确实被强制为逆序……
    assert completion == [1, 0], "未能构造出与 offset 相反的完成顺序，用例无意义"
    assert sorted(submits) == [0, 1]
    # ……但合并结果仍严格按 offset 升序，与完成顺序无关。
    assert text == "第1片文本\n第2片文本"
    assert metadata["task_ids"] == ["task-1", "task-2"]
    starts = [row["start"] for row in segments]
    assert starts == sorted(starts), "合并后的分片时间戳必须按升序"
    assert starts == [5.0, 605.0]


# --------------------------------------------------------------------------- #
# 2. real crash: subprocess hard-kill mid-submit
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_subprocess_hard_kill_mid_submit_freezes_and_never_repays(tmp_path):
    audio = tmp_path / "long.mp3"
    audio.write_bytes(b"\0" * 4096)
    cache = tmp_path / "long.chunks"

    crash_result = tmp_path / "crash.json"
    proc = subprocess.Popen(
        [sys.executable, str(CRASH_WORKER), "--audio", str(audio), "--mode", "crash",
         "--result", str(crash_result)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 40
        done_marker = cache / "chunk-00000.json"
        submitting_marker = cache / "chunk-00001.json"

        def _marker_state(path: Path) -> str | None:
            """Tolerant read: the child may be mid-write when we peek."""
            try:
                return _read_json(path).get("state")
            except (OSError, ValueError):
                return None

        while time.time() < deadline:
            # 两个分片是并发的（HAIKE_ASR_CONCURRENCY=2）⇒ chunk 1 的 submitting 标记
            # 完全可能早于 chunk 0 的 done 落盘。只等 chunk 1 会在 chunk 0 之前命中，
            # 于是 kill 掉一个「还没写完 done」的进程 —— 那是本用例的竞态误判，不是产品缺陷。
            # 必须两个条件同时成立才收网；chunk 1 的提交永不返回，不会错过窗口。
            if _marker_state(done_marker) == t.CHUNK_STATE_DONE and (
                _marker_state(submitting_marker) == t.CHUNK_STATE_SUBMITTING
            ):
                break
            if proc.poll() is not None:  # died on its own: unexpected
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only on a real timeout
            pytest.fail("子进程未在提交中途进入 submitting 状态")

        assert proc.poll() is None, "子进程不应自行结束：它应正阻塞在付费提交里"
        # hard-kill exactly while the paid submit is in flight
        proc.kill()
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()

    assert _read_json(done_marker)["state"] == t.CHUNK_STATE_DONE, "已完成分片应在崩溃前落盘"
    assert _read_json(submitting_marker)["state"] == t.CHUNK_STATE_SUBMITTING, "崩溃时受理不明标记必须在盘上"

    # restart in a fresh process: the submitting chunk must NOT be re-submitted
    restart_result = tmp_path / "restart.json"
    subprocess.run(
        [sys.executable, str(CRASH_WORKER), "--audio", str(audio), "--mode", "restart",
         "--result", str(restart_result)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True,
    )
    outcome = _read_json(restart_result)
    assert outcome["outcome"] == "raised"
    assert outcome["error_type"] == "TencentASRAmbiguous"
    assert outcome["status"] == "ambiguous"
    assert outcome["retryable"] is False
    assert outcome["submit_count"] == 0, "崩溃重启后绝不能重提受理不明的分片（零新增付费）"
    kept = _manifests(tmp_path)
    assert set(kept) == {"chunk-00000.json", "chunk-00001.json"}, "崩溃前后两份标记都应保留"
    assert kept["chunk-00000.json"]["state"] == t.CHUNK_STATE_DONE, "已付费完成的分片必须留档复用"
