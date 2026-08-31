"""Deterministic, editable PPT-style information cards for the workbench.

Cards are deliberately driven by a compact, human-confirmed content brief.
They never render the long visual-generation prompt used by image/video tools.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


CARD_TYPES = {
    "headline_metrics": "重点标题与要点",
    "comparison": "双项对比",
    "timeline": "流程与时间线",
}
THEMES = {
    "tech_neon": {"background": "#07162d", "panel": "#102d55", "accent": "#39d5ff", "text": "#f2fbff"},
    "editorial": {"background": "#11222e", "panel": "#243d4c", "accent": "#f7b84b", "text": "#fffaf0"},
    "signal_amber": {"background": "#21160b", "panel": "#44301a", "accent": "#ffbf45", "text": "#fff7e9"},
}


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _split_items(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").replace("；", "\n").replace(";", "\n").split("\n")
    result: list[str] = []
    for item in raw:
        cleaned = _clean_text(item, 26)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result[:4]


def _metric_pairs(value: Any) -> list[dict[str, str]]:
    raw = value if isinstance(value, list) else []
    result: list[dict[str, str]] = []
    for item in raw[:3]:
        if isinstance(item, dict):
            label, metric = _clean_text(item.get("label"), 18), _clean_text(item.get("value"), 18)
        else:
            label, _, metric = _clean_text(item, 40).partition(":")
            if not metric:
                label, _, metric = _clean_text(item, 40).partition("：")
        if label or metric:
            result.append({"label": label or "指标", "value": metric or "—"})
    return result


def default_safe_areas(presenter_treatment: str) -> dict[str, dict[str, float]]:
    areas: dict[str, dict[str, float]] = {"caption": {"x": 0.05, "y": 0.81, "width": 0.90, "height": 0.14}}
    if presenter_treatment in {"pip_top_left", "custom"}:
        areas["presenter"] = {"x": 0.0, "y": 0.0, "width": 0.34, "height": 0.34}
    return areas


def normalize_spec(payload: dict[str, Any], scene: dict[str, Any], width: int, height: int, presenter_treatment: str) -> dict[str, Any]:
    """Normalize the saved PPT brief into a compact render specification.

    ``summary`` is accepted as a legacy alias, but is intentionally limited to
    a short takeaway.  The visual-plan prompt is never read here.
    """
    card_type = str(payload.get("card_type") or "headline_metrics")
    if card_type not in CARD_TYPES:
        card_type = "headline_metrics"
    theme = str(payload.get("theme") or "tech_neon")
    if theme not in THEMES:
        theme = "tech_neon"
    title = _clean_text(payload.get("title") or scene.get("title"), 28) or "本段核心信息"
    takeaway = _clean_text(payload.get("takeaway") or payload.get("summary"), 48)
    items = _split_items(payload.get("items"))
    if len(items) < 2:
        source = _clean_text(scene.get("narration", {}).get("text") if isinstance(scene.get("narration"), dict) else scene.get("description"), 90)
        fallback = [source[:22], _clean_text(scene.get("shot_intent"), 22)]
        items = _split_items(items + fallback)
    while len(items) < 2:
        items.append("请补充本段关键要点")
    return {
        "version": 2,
        "card_type": card_type,
        "title": title,
        "summary": takeaway,
        "items": items[:4],
        "metrics": _metric_pairs(payload.get("metrics")),
        "theme": theme,
        "width": max(320, int(width)),
        "height": max(320, int(height)),
        "safe_areas": default_safe_areas(presenter_treatment),
        "source_scene_id": str(scene.get("id") or ""),
        "revision": max(1, int(payload.get("revision") or 1)),
    }


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in list(text or ""):
        candidate = current + character
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _draw_text_block(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, color: str, width: int, max_lines: int = 3, spacing: int = 8) -> int:
    x, y = xy
    for line in _wrap(draw, text, font, width)[:max_lines]:
        draw.text((x, y), line, font=font, fill=color)
        y += int(getattr(font, "size", 22) * 1.22) + spacing
    return y


def _draw_item_rows(draw: ImageDraw.ImageDraw, spec: dict[str, Any], colors: dict[str, str], left: int, top: int, width: int, bottom: int) -> None:
    items = spec["items"]
    row_gap = max(12, width // 56)
    row_height = max(78, (bottom - top - row_gap * (len(items) - 1)) // len(items))
    body_font = _font(max(21, width // 30))
    small_font = _font(max(16, width // 48))
    for index, item in enumerate(items):
        y = top + index * (row_height + row_gap)
        draw.rounded_rectangle((left, y, left + width, y + row_height), radius=16, fill=colors["panel"], outline="#2f5d88", width=2)
        draw.ellipse((left + 18, y + max(17, row_height // 2 - 14), left + 46, y + max(45, row_height // 2 + 14)), fill=colors["accent"])
        draw.text((left + 27, y + max(20, row_height // 2 - 12)), str(index + 1), font=small_font, fill=colors["background"])
        _draw_text_block(draw, (left + 64, y + max(18, row_height // 4)), item, body_font, colors["text"], width - 82, max_lines=2, spacing=2)


def _draw_card(spec: dict[str, Any], output: Path) -> None:
    width, height = int(spec["width"]), int(spec["height"])
    colors = THEMES[spec["theme"]]
    image = Image.new("RGB", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)
    for index in range(8):
        offset = int((index + 1) * width / 9)
        draw.line((offset, 0, offset - height // 2, height), fill=colors["panel"], width=max(2, width // 420))

    margin = max(32, width // 22)
    safe = spec["safe_areas"].get("presenter")
    content_left = margin
    if safe:
        content_left = max(content_left, int(width * (safe["x"] + safe["width"])) + margin // 2)
    content_width = max(width // 3, width - content_left - margin)
    title_font = _font(max(30, min(58, width // 19)), bold=True)
    body_font = _font(max(20, min(34, width // 35)))
    small_font = _font(max(16, min(24, width // 48)))
    accent = colors["accent"]

    draw.rounded_rectangle((content_left, margin, width - margin, margin + max(8, height // 120)), radius=6, fill=accent)
    title_bottom = _draw_text_block(draw, (content_left, margin + max(28, height // 42)), spec["title"], title_font, colors["text"], content_width, max_lines=2)
    content_top = title_bottom + max(10, height // 82)
    if spec["summary"]:
        draw.rounded_rectangle((content_left, content_top, width - margin, content_top + max(72, height // 13)), radius=13, fill="#0a2341")
        content_top = _draw_text_block(draw, (content_left + 18, content_top + 14), spec["summary"], body_font, "#c8e8ff", content_width - 36, max_lines=2, spacing=2) + max(18, height // 80)

    content_bottom = int(height * 0.74)
    if spec["card_type"] == "comparison":
        gap = max(14, width // 64)
        box_width = (content_width - gap) // 2
        for index, item in enumerate((spec["items"] + ["待补充"] * 2)[:2]):
            x = content_left + index * (box_width + gap)
            draw.rounded_rectangle((x, content_top, x + box_width, content_bottom), radius=18, fill=colors["panel"], outline=accent, width=2)
            draw.text((x + 20, content_top + 18), f"0{index + 1}", font=small_font, fill=accent)
            _draw_text_block(draw, (x + 20, content_top + 56), item, body_font, colors["text"], box_width - 40, max_lines=4)
    elif spec["card_type"] == "timeline":
        steps = spec["items"][:3]
        row_height = max(68, (content_bottom - content_top) // len(steps))
        for index, item in enumerate(steps):
            y = content_top + index * row_height
            draw.ellipse((content_left, y, content_left + 42, y + 42), fill=accent)
            draw.text((content_left + 13, y + 7), str(index + 1), font=small_font, fill=colors["background"])
            _draw_text_block(draw, (content_left + 62, y + 1), item, body_font, colors["text"], content_width - 62, max_lines=2)
    elif spec["metrics"]:
        metrics = spec["metrics"]
        gap = max(12, width // 64)
        cols = 2 if len(metrics) > 1 else 1
        card_width = (content_width - gap * (cols - 1)) // cols
        card_height = min(max(104, height // 9), max(104, content_bottom - content_top))
        for index, metric in enumerate(metrics):
            x = content_left + (index % cols) * (card_width + gap)
            y = content_top + (index // cols) * (card_height + gap)
            draw.rounded_rectangle((x, y, x + card_width, y + card_height), radius=16, fill=colors["panel"], outline="#2f5d88", width=2)
            draw.text((x + 18, y + 16), metric["value"], font=body_font, fill=accent)
            _draw_text_block(draw, (x + 18, y + card_height // 2), metric["label"], small_font, colors["text"], card_width - 36, max_lines=2, spacing=2)
        remaining_top = content_top + ((len(metrics) + cols - 1) // cols) * (card_height + gap)
        if remaining_top + 90 < content_bottom:
            _draw_item_rows(draw, spec, colors, content_left, remaining_top, content_width, content_bottom)
    else:
        _draw_item_rows(draw, spec, colors, content_left, content_top, content_width, content_bottom)

    draw.text((margin, int(height * .94)), "信息卡素材 · 可在时间线中独立替换、锁定或拆分", font=small_font, fill="#8fb8cf")
    image.save(output, "PNG", optimize=True)


def _svg(spec: dict[str, Any]) -> str:
    width, height = int(spec["width"]), int(spec["height"])
    colors = THEMES[spec["theme"]]
    safe = spec["safe_areas"].get("presenter")
    left = int(width * (safe["x"] + safe["width"]) + width * .04) if safe else int(width * .06)
    title = html.escape(spec["title"])
    summary = html.escape(spec["summary"])
    lines = []
    for index, item in enumerate(spec["items"][:4]):
        lines.append(f'<text x="{left}" y="{int(height * .46) + index * 66}" font-size="36" fill="{colors["text"]}">• {html.escape(item)}</text>')
    presenter = ""
    if safe:
        presenter = f'<rect x="0" y="0" width="{int(width * safe["width"])}" height="{int(height * safe["height"])}" fill="#000000" opacity=".10"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="{colors["background"]}"/>
<rect x="{left}" y="{int(height * .06)}" width="{width - left - int(width * .06)}" height="12" rx="6" fill="{colors["accent"]}"/>
{presenter}
<text x="{left}" y="{int(height * .18)}" font-size="64" font-weight="700" fill="{colors["text"]}">{title}</text>
<text x="{left}" y="{int(height * .31)}" font-size="32" fill="#c8e8ff">{summary}</text>
{''.join(lines)}
</svg>'''


def render_card(output_directory: Path, card_id: str, spec: dict[str, Any]) -> dict[str, Path]:
    """Render local card assets. The caller owns ledger registration."""
    output_directory.mkdir(parents=True, exist_ok=True)
    png_path = output_directory / "card.png"
    svg_path = output_directory / "card.svg"
    spec_path = output_directory / "card.json"
    _draw_card(spec, png_path)
    svg_path.write_text(_svg(spec), encoding="utf-8")
    spec_path.write_text(json.dumps({"card_id": card_id, **spec}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"png": png_path, "svg": svg_path, "spec": spec_path}
