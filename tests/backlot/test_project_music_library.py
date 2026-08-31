import shutil
import subprocess
from pathlib import Path

import pytest

from backlot.music_library import (
    MusicLibraryError,
    complete_project_music_upload,
    list_music_tracks,
    prepare_project_music_upload,
    resolve_music_track,
)


def _project(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _fake_probe(monkeypatch, duration: float = 3.0) -> None:
    monkeypatch.setattr("backlot.music_library._probe", lambda _p: {
        "duration_seconds": duration, "codec": "pcm_s16le", "sample_rate": 48000, "channels": 1,
    })


def test_prepare_rejects_extension_and_sanitizes_name(tmp_path):
    project = _project(tmp_path, "p")
    with pytest.raises(MusicLibraryError, match="不支持"):
        prepare_project_music_upload(project, "payload.exe")
    temporary = prepare_project_music_upload(project, "../../我的?音乐.wav")
    assert temporary.parent == project / "assets/audio/music/uploads"
    assert ".." not in temporary.name


def test_complete_rejects_empty_oversize_undecodable_and_outside(tmp_path, monkeypatch):
    project = _project(tmp_path, "p")
    empty = prepare_project_music_upload(project, "empty.wav")
    with pytest.raises(MusicLibraryError, match="为空"):
        complete_project_music_upload(project, empty, "empty.wav")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"audio")
    with pytest.raises(MusicLibraryError, match="安全临时目录"):
        complete_project_music_upload(project, outside, "outside.wav")
    too_big = prepare_project_music_upload(project, "large.wav")
    too_big.write_bytes(b"12345")
    with pytest.raises(MusicLibraryError, match="超过"):
        complete_project_music_upload(project, too_big, "large.wav", max_bytes=4)
    broken = prepare_project_music_upload(project, "broken.wav")
    broken.write_bytes(b"not audio")
    monkeypatch.setattr("backlot.music_library._probe", lambda _p: (_ for _ in ()).throw(MusicLibraryError("背景音乐无法解码")))
    with pytest.raises(MusicLibraryError, match="无法解码"):
        complete_project_music_upload(project, broken, "broken.wav")


def test_hash_id_is_idempotent_and_project_scoped(tmp_path, monkeypatch):
    _fake_probe(monkeypatch)
    first_project = _project(tmp_path, "one")
    second_project = _project(tmp_path, "two")
    first = prepare_project_music_upload(first_project, "one.wav")
    first.write_bytes(b"same-content")
    path1, track1 = complete_project_music_upload(first_project, first, "one.wav")
    duplicate = prepare_project_music_upload(first_project, "renamed.wav")
    duplicate.write_bytes(b"same-content")
    path2, track2 = complete_project_music_upload(first_project, duplicate, "renamed.wav")
    assert path1 == path2
    assert track1["id"] == track2["id"]
    assert len(list((first_project / "assets/audio/music/uploads").glob("project-music-*.wav"))) == 1
    assert resolve_music_track(track1["id"], first_project)[0] == path1
    with pytest.raises(MusicLibraryError):
        resolve_music_track(track1["id"], second_project)
    with pytest.raises(MusicLibraryError):
        resolve_music_track(track1["id"])
    assert all(item["id"] != track1["id"] for item in list_music_tracks(second_project)["tracks"])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
def test_real_ffprobe_accepts_short_audio(tmp_path):
    project = _project(tmp_path, "real")
    generated = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(generated)],
        check=True, capture_output=True, timeout=30,
    )
    temporary = prepare_project_music_upload(project, "tone.wav")
    shutil.copyfile(generated, temporary)
    stored, metadata = complete_project_music_upload(project, temporary, "tone.wav")
    assert stored.is_file()
    assert metadata["duration_seconds"] >= 1.0
    assert metadata["content_sha256"] in metadata["id"]
