"""Pexels candidate previews must be available before any media download."""
from __future__ import annotations

from tools.video.stock_sources.base import SearchFilters
from tools.video.stock_sources.pexels import PexelsSource


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "videos": [{
                "id": 34775736, "width": 1080, "height": 1920, "duration": 12,
                "url": "https://www.pexels.com/video/robot-34775736/",
                "image": "https://images.pexels.com/cover.jpg",
                "user": {"name": "Pexels Creator", "url": "https://pexels.com/@creator"},
                "video_pictures": [{"picture": "https://images.pexels.com/frame-1.jpg"}, {"picture": "https://images.pexels.com/frame-2.jpg"}],
                "video_files": [{"file_type": "video/mp4", "width": 1080, "height": 1920, "quality": "hd", "fps": 30, "link": "https://videos.pexels.com/final.mp4"}],
            }],
        }


def test_pexels_search_returns_preview_metadata_without_download(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    candidates = PexelsSource().search("industrial robot factory", SearchFilters(
        kind="video", orientation="portrait", min_duration=5, per_page=6,
    ))

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/videos/search")
    assert candidates[0].thumbnail_url == "https://images.pexels.com/cover.jpg"
    assert candidates[0].extra["preview_frames"] == [
        "https://images.pexels.com/frame-1.jpg", "https://images.pexels.com/frame-2.jpg",
    ]
    assert candidates[0].download_url.endswith("final.mp4")
