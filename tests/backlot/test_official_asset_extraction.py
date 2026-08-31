"""Unit contracts for official press-image extraction (og:image) and its bridge."""
from __future__ import annotations

import backlot.daily_automation as daily_automation
from backlot.news_selection_v2 import _first_official_image
from backlot.visual_director import official_image_candidate


def test_extract_og_image_resolves_relative_and_attribution():
    html = (
        '<html><head>'
        '<meta property="og:image" content="/img/hero.jpg">'
        '<meta property="og:site_name" content="IT之家">'
        "</head><body></body></html>"
    )
    result = daily_automation._extract_article_og_image(html, base_url="https://www.ithome.com/0/1.html")
    assert result["image_url"] == "https://www.ithome.com/img/hero.jpg"
    assert result["attribution"] == "IT之家"


def test_extract_og_image_falls_back_to_twitter_image():
    html = '<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">'
    result = daily_automation._extract_article_og_image(html)
    assert result["image_url"] == "https://cdn.example.com/tw.jpg"
    assert result["attribution"] == ""


def test_extract_og_image_returns_empty_when_none_present():
    result = daily_automation._extract_article_og_image("<html><body>no images</body></html>")
    assert result["image_url"] == ""
    assert result["attribution"] == ""


def test_enrich_candidate_attaches_official_image(monkeypatch):
    class FakeResponse:
        encoding = "utf-8"
        apparent_encoding = "utf-8"
        text = '<meta property="og:image" content="https://cdn.example.com/pic.jpg"><p>' + ("x" * 200) + "</p>"

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, timeout=20, headers=None):
            return FakeResponse()

    monkeypatch.setattr(daily_automation.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(daily_automation, "_decode_google_news_url", lambda url, session=None: url)

    candidate = {"url": "https://example.com/article", "title": "某硬件发布"}
    daily_automation._enrich_candidate_evidence(candidate)
    assert candidate["evidence_status"] == "ok"
    assert candidate["evidence_image_url"] == "https://cdn.example.com/pic.jpg"
    assert candidate["evidence_image_license"] == "press"
    assert candidate["evidence_image_attribution"] == "example.com"


def test_first_official_image_resolves_from_members():
    event = {
        "members": [
            {"evidence_image_url": "https://img/a.png", "evidence_image_attribution": "量子位"},
        ]
    }
    assert _first_official_image(event) == {"url": "https://img/a.png", "attribution": "量子位"}
    assert _first_official_image({"members": [{"evidence_excerpt": "只有正文，无图"}]}) == {}


def test_official_image_candidate_shape():
    candidate = official_image_candidate(
        image_url="https://img/o.png", attribution="量子位", title="某大模型发布", source_url="https://a.com/1",
    )
    assert candidate.source == "official_press"
    assert candidate.kind == "image"
    assert candidate.license == "press"
    assert candidate.creator == "量子位"
    assert candidate.download_url == "https://img/o.png"
    assert candidate.source_url == "https://a.com/1"
    assert candidate.extra.get("official") is True
    assert "官方配图" in candidate.source_tags


def test_probe_official_image_rejects_tiny_placeholder(tmp_path):
    import backlot.workbench as workbench

    tiny = tmp_path / "favicon.jpg"
    tiny.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 12)
    quality = workbench._probe_official_image(tiny)
    assert quality["reject"] is True
    assert any("文件过小" in reason for reason in quality["reasons"])
