import json
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose


def test_cinematic_cut_adapter_builds_a_sequential_timeline() -> None:
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        {
            "id": "v1",
            "source": "clip.mp4",
            "in_seconds": 2,
            "out_seconds": 6,
            "transition_in": "cut",
            "transition_out": "none",
        },
        {
            "id": "title",
            "source": "",
            "type": "hero_title",
            "text": "The signal arrives",
            "in_seconds": 0,
            "out_seconds": 3,
        },
    ])

    assert scenes[0] == {
        "id": "v1",
        "startSeconds": 0.0,
        "durationSeconds": 4.0,
        "kind": "video",
        "src": "clip.mp4",
        "trimBeforeSeconds": 2.0,
        "trimAfterSeconds": 6.0,
        "playbackRate": 1.0,
        "fadeInFrames": 0,
        "fadeOutFrames": 0,
    }
    assert scenes[1]["kind"] == "title"
    assert scenes[1]["startSeconds"] == 4.0
    assert scenes[1]["text"] == "The signal arrives"


def test_cinematic_cut_adapter_preserves_playback_speed() -> None:
    scenes = VideoCompose._cuts_to_cinematic_scenes([
        {
            "id": "fast",
            "source": "clip.mp4",
            "in_seconds": 2,
            "out_seconds": 6,
            "speed": 2,
        }
    ])

    assert scenes[0]["durationSeconds"] == 2
    assert scenes[0]["playbackRate"] == 2


@pytest.mark.parametrize("uri_style", ["standard", "legacy_windows"])
def test_remotion_media_staging_decodes_file_uris(tmp_path, uri_style) -> None:
    source = tmp_path / "clip with space.mp4"
    source.write_bytes(b"video")
    public_dir = tmp_path / "public"
    uri = source.as_uri()
    if uri_style == "legacy_windows" and len(source.drive) == 2:
        uri = f"file://{source.drive}{source.as_posix()[2:]}".replace(" ", "%20")
    props = {"scenes": [{"src": uri}]}

    staged_count = VideoCompose._stage_remotion_media(props, public_dir)

    assert staged_count == 1
    assert props["scenes"][0]["src"] != uri
    assert (public_dir / props["scenes"][0]["src"]).read_bytes() == b"video"


def test_remotion_render_adapts_cuts_and_stages_local_video(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not-a-real-video")
    output = tmp_path / "render.mp4"
    captured = {}

    def fake_run_command(self, command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        props_arg = next(arg for arg in command if arg.startswith("--props="))
        captured["props"] = json.loads(Path(props_arg.split("=", 1)[1]).read_text())
        public_arg = next(arg for arg in command if arg.startswith("--public-dir="))
        public_dir = Path(public_arg.split("=", 1)[1])
        captured["staged_exists_during_render"] = (
            public_dir / captured["props"]["scenes"][0]["src"]
        ).exists()
        output.write_bytes(b"rendered")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)

    result = VideoCompose()._remotion_render({
        "edit_decisions": {
            "renderer_family": "cinematic-trailer",
            "cuts": [
                {
                    "id": "v1",
                    "source": str(source),
                    "in_seconds": 0,
                    "out_seconds": 2,
                }
            ],
        },
        "output_path": str(output),
    })

    assert result.success, result.error
    assert "cuts" not in captured["props"]
    assert captured["props"]["scenes"][0]["kind"] == "video"
    assert captured["staged_exists_during_render"] is True
    assert result.data["staged_media_count"] == 1


def test_remotion_timeout_scales_with_scene_count(monkeypatch, tmp_path) -> None:
    output = tmp_path / "render.mp4"
    captured = {}

    def fake_run_command(self, command, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        output.write_bytes(b"rendered")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)
    cuts = [
        {
            "id": f"title-{index}",
            "source": "",
            "type": "hero_title",
            "text": str(index),
            "in_seconds": 0,
            "out_seconds": 1,
        }
        for index in range(50)
    ]

    result = VideoCompose()._remotion_render({
        "edit_decisions": {"renderer_family": "cinematic-trailer", "cuts": cuts},
        "output_path": str(output),
    })

    assert result.success, result.error
    assert captured["timeout"] == 750


def test_remotion_render_preserves_direct_cinematic_scenes(monkeypatch, tmp_path) -> None:
    output = tmp_path / "render.mp4"
    captured = {}

    def fake_run_command(self, command, **kwargs):
        props_arg = next(arg for arg in command if arg.startswith("--props="))
        captured["props"] = json.loads(Path(props_arg.split("=", 1)[1]).read_text())
        output.write_bytes(b"rendered")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)
    scene = {
        "id": "authored",
        "kind": "title",
        "text": "Keep me",
        "startSeconds": 0,
        "durationSeconds": 1,
    }

    result = VideoCompose()._remotion_render({
        "composition_data": {
            "renderer_family": "cinematic-trailer",
            "scenes": [scene],
        },
        "output_path": str(output),
    })

    assert result.success, result.error
    assert captured["props"]["scenes"] == [scene]


def test_layered_content_routes_to_cinematic_renderer_and_stages_nested_media(monkeypatch, tmp_path) -> None:
    base = tmp_path / "base.mp4"
    hero = tmp_path / "hero.png"
    base.write_bytes(b"base")
    hero.write_bytes(b"hero")
    output = tmp_path / "layered.mp4"
    captured = {}

    def fake_run_command(self, command, **kwargs):
        captured["command"] = command
        props_arg = next(arg for arg in command if arg.startswith("--props="))
        captured["props"] = json.loads(Path(props_arg.split("=", 1)[1]).read_text())
        output.write_bytes(b"rendered")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)
    result = VideoCompose()._remotion_render({
        "composition_data": {
            "renderer_family": "layered-content",
            "canvasWidth": 1080,
            "canvasHeight": 1920,
            "frameRate": 24,
            "scenes": [{
                "id": "layered",
                "kind": "layered",
                "startSeconds": 0,
                "durationSeconds": 2,
                "layoutRecipe": "focus_card",
                "background": {"src": str(base), "mediaType": "video", "fit": "cover"},
                "overlays": [{
                    "id": "VL-001", "role": "hero", "src": str(hero), "mediaType": "image",
                    "startSeconds": .5, "endSeconds": 1.5, "fit": "contain",
                    "muted": True, "playbackRate": 1,
                    "placement": {
                        "presetId": "landscape_hero_center", "positionXRatio": .5,
                        "positionYRatio": .47, "sizeRatio": .74,
                        "aspectMode": "source", "maxHeightRatio": .78,
                        "sourceAspectRatio": 16 / 9,
                    },
                }],
            }],
        },
        "output_path": str(output),
    })

    assert result.success, result.error
    assert "CinematicRenderer" in captured["command"]
    scene = captured["props"]["scenes"][0]
    assert scene["background"]["src"] != str(base)
    assert scene["overlays"][0]["src"] != str(hero)
    assert scene["overlays"][0]["muted"] is True
    assert scene["overlays"][0]["playbackRate"] == 1
    assert scene["overlays"][0]["placement"]["positionXRatio"] == .5
    assert scene["overlays"][0]["placement"]["sourceAspectRatio"] == 16 / 9
    assert result.data["staged_media_count"] == 2
