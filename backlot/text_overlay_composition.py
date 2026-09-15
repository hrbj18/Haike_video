"""Project-level multi-layer text overlay contract and local raster assets.

The module deliberately has no workbench dependency.  It can therefore be
used by scene previews, whole-film previews, final renders, and focused tests
without creating a second state or rendering implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


TEXT_OVERLAY_VERSION = 1
TEXT_OVERLAY_MAX_LAYERS = 64
TEXT_OVERLAY_SAFE_INSET = 0.05
TEXT_OVERLAY_ANIMATIONS = {
    "none", "fade", "slide_up", "slide_down", "slide_left", "slide_right", "scale",
}
TEXT_OVERLAY_ALIGNMENTS = {"left", "center", "right"}


class TextOverlayValidationError(ValueError):
    """A user-correctable text overlay contract validation failure."""


def empty_text_overlay_composition() -> dict[str, Any]:
    return {"version": TEXT_OVERLAY_VERSION, "revision": 0, "layers": []}


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TextOverlayValidationError(f"{label} 必须是数字") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise TextOverlayValidationError(f"{label} 必须是有限数字")
    return result


def _bounded_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    result = _number(value, label)
    if result < minimum or result > maximum:
        raise TextOverlayValidationError(f"{label} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    result = _number(value, label)
    if not result.is_integer() or result < minimum or result > maximum:
        raise TextOverlayValidationError(f"{label} 必须是 {minimum} 到 {maximum} 的整数")
    return int(result)


def _color(value: Any, label: str, default: str) -> str:
    candidate = str(value or default).strip().upper()
    if not re.fullmatch(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?", candidate):
        raise TextOverlayValidationError(f"{label} 必须是 #RRGGBB 或 #RRGGBBAA")
    return candidate


def _animation(value: Any, label: str) -> str:
    candidate = str(value or "none").strip().lower()
    if candidate not in TEXT_OVERLAY_ANIMATIONS:
        raise TextOverlayValidationError(f"{label} 不支持：{candidate}")
    return candidate


def normalize_text_overlay_layer(raw: Any, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TextOverlayValidationError(f"第 {index + 1} 个标题图层必须是对象")
    layer = deepcopy(raw)
    layer_id = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", layer_id):
        raise TextOverlayValidationError(f"第 {index + 1} 个标题图层缺少合法稳定 ID")
    text = str(raw.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise TextOverlayValidationError(f"{layer_id} 文案不能为空")
    if len(text) > 1000 or len(text.split("\n")) > 20:
        raise TextOverlayValidationError(f"{layer_id} 文案过长")

    start = _bounded_number(raw.get("start_seconds", 0), f"{layer_id}.start_seconds", 0, 86400)
    end = _bounded_number(raw.get("end_seconds", 0), f"{layer_id}.end_seconds", 0, 86400)
    if end <= start:
        raise TextOverlayValidationError(f"{layer_id} 的结束时间必须晚于开始时间")

    x = _bounded_number(raw.get("x", 0.1), f"{layer_id}.x", 0, 1)
    y = _bounded_number(raw.get("y", 0.1), f"{layer_id}.y", 0, 1)
    width = _bounded_number(raw.get("width", 0.8), f"{layer_id}.width", 0.01, 1)
    height = _bounded_number(raw.get("height", 0.12), f"{layer_id}.height", 0.01, 1)
    if x + width > 1.000001 or y + height > 1.000001:
        raise TextOverlayValidationError(f"{layer_id} 的归一化矩形超出画布")

    align = str(raw.get("text_align") or "center").strip().lower()
    if align not in TEXT_OVERLAY_ALIGNMENTS:
        raise TextOverlayValidationError(f"{layer_id}.text_align 只能是 left、center 或 right")
    enter = _animation(raw.get("enter_animation"), f"{layer_id}.enter_animation")
    exit_animation = _animation(raw.get("exit_animation"), f"{layer_id}.exit_animation")
    enter_duration = _bounded_number(raw.get("enter_duration_seconds", 0), f"{layer_id}.enter_duration_seconds", 0, 3)
    exit_duration = _bounded_number(raw.get("exit_duration_seconds", 0), f"{layer_id}.exit_duration_seconds", 0, 3)
    if enter == "none":
        enter_duration = 0.0
    if exit_animation == "none":
        exit_duration = 0.0
    if enter_duration + exit_duration > end - start + 0.000001:
        raise TextOverlayValidationError(f"{layer_id} 的入场和退场动画总时长超过可见时长")

    layer.update({
        "id": layer_id,
        "text": text,
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
        "font_family": str(raw.get("font_family") or "Microsoft YaHei").strip()[:120],
        "font_size": round(_bounded_number(raw.get("font_size", 56), f"{layer_id}.font_size", 8, 400), 3),
        "font_weight": _integer(raw.get("font_weight", 700), f"{layer_id}.font_weight", 100, 900),
        "font_variation": str(raw.get("font_variation") or "").strip()[:40],
        "color": _color(raw.get("color"), f"{layer_id}.color", "#FFFFFF"),
        "stroke_color": _color(raw.get("stroke_color"), f"{layer_id}.stroke_color", "#111111"),
        "stroke_width": round(_bounded_number(raw.get("stroke_width", 0), f"{layer_id}.stroke_width", 0, 30), 3),
        "shadow_color": _color(raw.get("shadow_color"), f"{layer_id}.shadow_color", "#00000080"),
        "shadow_blur": round(_bounded_number(raw.get("shadow_blur", 0), f"{layer_id}.shadow_blur", 0, 60), 3),
        "shadow_offset_x": round(_bounded_number(raw.get("shadow_offset_x", 0), f"{layer_id}.shadow_offset_x", -100, 100), 3),
        "shadow_offset_y": round(_bounded_number(raw.get("shadow_offset_y", 0), f"{layer_id}.shadow_offset_y", -100, 100), 3),
        "line_height": round(_bounded_number(raw.get("line_height", 1.15), f"{layer_id}.line_height", 0.6, 3), 3),
        "text_align": align,
        "background_color": _color(raw.get("background_color"), f"{layer_id}.background_color", "#000000"),
        "background_opacity": round(_bounded_number(raw.get("background_opacity", 0), f"{layer_id}.background_opacity", 0, 1), 4),
        "background_radius": round(_bounded_number(raw.get("background_radius", 0), f"{layer_id}.background_radius", 0, 400), 3),
        "padding_x": round(_bounded_number(raw.get("padding_x", 16), f"{layer_id}.padding_x", 0, 300), 3),
        "padding_y": round(_bounded_number(raw.get("padding_y", 10), f"{layer_id}.padding_y", 0, 300), 3),
        "enter_animation": enter,
        "enter_duration_seconds": round(enter_duration, 3),
        "exit_animation": exit_animation,
        "exit_duration_seconds": round(exit_duration, 3),
        "z_index": _integer(raw.get("z_index", index), f"{layer_id}.z_index", -10000, 10000),
        "locked": bool(raw.get("locked", False)),
    })
    return layer


def normalize_text_overlay_composition(raw: Any) -> dict[str, Any]:
    if raw is None or raw is False:
        return empty_text_overlay_composition()
    if not isinstance(raw, dict):
        raise TextOverlayValidationError("text_overlay_composition 必须是对象")
    version = _integer(raw.get("version", TEXT_OVERLAY_VERSION), "text_overlay_composition.version", 1, TEXT_OVERLAY_VERSION)
    revision = _integer(raw.get("revision", 0), "text_overlay_composition.revision", 0, 2_147_483_647)
    source_layers = raw.get("layers", [])
    if not isinstance(source_layers, list):
        raise TextOverlayValidationError("text_overlay_composition.layers 必须是数组")
    if len(source_layers) > TEXT_OVERLAY_MAX_LAYERS:
        raise TextOverlayValidationError(f"标题图层不能超过 {TEXT_OVERLAY_MAX_LAYERS} 层")
    layers = [normalize_text_overlay_layer(item, index) for index, item in enumerate(source_layers)]
    ids = [item["id"] for item in layers]
    if len(ids) != len(set(ids)):
        raise TextOverlayValidationError("标题图层稳定 ID 不能重复")
    result = deepcopy(raw)
    result.update({"version": version, "revision": revision, "layers": layers})
    return result


def assert_locked_layer_transition(previous: dict[str, Any], current: dict[str, Any]) -> None:
    old_layers = previous.get("layers", [])
    new_layers = current.get("layers", [])
    old_by_id = {item["id"]: item for item in old_layers}
    new_by_id = {item["id"]: item for item in new_layers}
    unlocked_ids: list[str] = []
    for index, old in enumerate(old_layers):
        if not old.get("locked"):
            continue
        new = new_by_id.get(old["id"])
        if new is None:
            raise TextOverlayValidationError(f"锁定图层 {old['id']} 不能删除")
        old_compare = deepcopy(old)
        new_compare = deepcopy(new)
        old_compare["locked"] = False
        new_compare["locked"] = False
        if old_compare != new_compare or new_layers.index(new) != index:
            raise TextOverlayValidationError(f"锁定图层 {old['id']} 不能修改或排序；请先解锁")
        if not new.get("locked"):
            unlocked_ids.append(old["id"])
    if unlocked_ids:
        expected = deepcopy(previous)
        for layer in expected.get("layers", []):
            if layer.get("id") in unlocked_ids:
                layer["locked"] = False
        if expected.get("layers") != current.get("layers"):
            raise TextOverlayValidationError("解锁必须单独保存，不能同时修改其他标题图层")


def composition_layers_for_window(composition: dict[str, Any], start_seconds: float, end_seconds: float) -> list[dict[str, Any]]:
    """Clip project-clock layers to a local render window."""
    result: list[dict[str, Any]] = []
    for order, layer in enumerate(composition.get("layers", [])):
        start = max(float(layer["start_seconds"]), float(start_seconds))
        end = min(float(layer["end_seconds"]), float(end_seconds))
        if end <= start:
            continue
        item = deepcopy(layer)
        item["start_seconds"] = round(start - start_seconds, 6)
        item["end_seconds"] = round(end - start_seconds, 6)
        # Preserve the progress of animations that began before the window.
        if float(layer["start_seconds"]) < start_seconds:
            item["enter_animation"] = "none"
            item["enter_duration_seconds"] = 0.0
        if float(layer["end_seconds"]) > end_seconds:
            item["exit_animation"] = "none"
            item["exit_duration_seconds"] = 0.0
        item["_order"] = order
        result.append(item)
    return result


def safe_zone_warnings(composition: dict[str, Any], inset: float = TEXT_OVERLAY_SAFE_INSET) -> list[dict[str, str]]:
    warnings = []
    for layer in composition.get("layers", []):
        if (layer["x"] < inset or layer["y"] < inset
                or layer["x"] + layer["width"] > 1 - inset
                or layer["y"] + layer["height"] > 1 - inset):
            warnings.append({"layer_id": layer["id"], "code": "outside_safe_zone"})
    return warnings


def _rgba(value: str, opacity_multiplier: float = 1.0) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    alpha = int(value[6:8], 16) if len(value) == 8 else 255
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), round(alpha * opacity_multiplier)


def _font_path(font_family: str, bold: bool) -> Path | None:
    root = Path("C:/Windows/Fonts")
    family = font_family.lower()
    candidates: list[str]
    if "noto" in family or "思源" in family or "source han" in family:
        # Noto Sans SC ships as a variable font; weight is selected by _font_variation().
        candidates = ["NotoSansSC-VF.ttf", "msyhbd.ttc", "msyh.ttc"]
    elif "yahei" in family or "雅黑" in family:
        candidates = ["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhbd.ttc"]
    elif "deng" in family or "等线" in family:
        candidates = ["Dengb.ttf", "Deng.ttf", "msyhbd.ttc"] if bold else ["Deng.ttf", "Dengb.ttf", "msyh.ttc"]
    elif "simhei" in family or "黑体" in family:
        candidates = ["simhei.ttf", "msyhbd.ttc"]
    elif "simsun" in family or "宋体" in family:
        candidates = ["simsun.ttc", "msyh.ttc"]
    else:
        candidates = ["msyhbd.ttc", "arialbd.ttf", "msyh.ttc"] if bold else ["msyh.ttc", "arial.ttf"]
    return next((root / name for name in candidates if (root / name).is_file()), None)


_DEFAULT_VARIATION_BY_WEIGHT = (
    (900, "Black"),
    (800, "ExtraBold"),
    (700, "Bold"),
    (600, "SemiBold"),
    (500, "Medium"),
)


def _font_variation(layer: dict[str, Any]) -> str:
    """Resolve the variable-font instance name for a layer.

    An explicit ``font_variation`` always wins; otherwise the weight is mapped
    onto Noto Sans SC's named instances so a 900-weight layer really renders
    black instead of silently falling back to the font's Regular instance.
    """
    explicit = str(layer.get("font_variation") or "").strip()
    if explicit:
        return explicit
    family = str(layer.get("font_family") or "").lower()
    if "noto" not in family and "思源" not in family and "source han" not in family:
        return ""
    weight = int(layer.get("font_weight") or 400)
    for threshold, name in _DEFAULT_VARIATION_BY_WEIGHT:
        if weight >= threshold:
            return name
    return ""


def _apply_variation(font: Any, variation: str) -> Any:
    if not variation or not hasattr(font, "set_variation_by_name"):
        return font
    try:
        font.set_variation_by_name(variation)
    except (ValueError, OSError, TypeError):
        return font
    return font


def _draw_text_asset(path: Path, layer: dict[str, Any], pixel_width: int, pixel_height: int, scale: float) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    image = Image.new("RGBA", (pixel_width, pixel_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(0, round(layer["background_radius"] * scale))
    background = _rgba(layer["background_color"], layer["background_opacity"])
    if background[3] > 0:
        draw.rounded_rectangle((0, 0, pixel_width - 1, pixel_height - 1), radius=radius, fill=background)

    font_size = max(1, round(layer["font_size"] * scale))
    font_path = _font_path(layer["font_family"], layer["font_weight"] >= 600)
    font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default()
    font = _apply_variation(font, _font_variation(layer))
    stroke = max(0, round(layer["stroke_width"] * scale))
    pad_x = max(0, round(layer["padding_x"] * scale))
    pad_y = max(0, round(layer["padding_y"] * scale))
    lines = layer["text"].split("\n")
    sample_bbox = draw.textbbox((0, 0), "Hg国", font=font, stroke_width=stroke)
    natural_height = max(1, sample_bbox[3] - sample_bbox[1])
    line_step = max(1, round(font_size * layer["line_height"]))
    block_height = natural_height + line_step * (len(lines) - 1)
    top = pad_y + max(0, (pixel_height - 2 * pad_y - block_height) // 2) - sample_bbox[1]

    placements: list[tuple[float, float, str]] = []
    for line_index, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=stroke)
        text_width = bbox[2] - bbox[0]
        if layer["text_align"] == "left":
            left = pad_x - bbox[0]
        elif layer["text_align"] == "right":
            left = pixel_width - pad_x - text_width - bbox[0]
        else:
            left = (pixel_width - text_width) / 2 - bbox[0]
        placements.append((left, top + line_index * line_step, line))

    shadow_blur = max(0, round(layer["shadow_blur"] * scale))
    shadow_offset_x = round(layer["shadow_offset_x"] * scale)
    shadow_offset_y = round(layer["shadow_offset_y"] * scale)
    shadow_fill = _rgba(layer["shadow_color"])
    if shadow_fill[3] > 0 and (shadow_blur or shadow_offset_x or shadow_offset_y):
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        for left, line_top, line in placements:
            shadow_draw.text((left + shadow_offset_x, line_top + shadow_offset_y), line, font=font,
                             fill=shadow_fill, stroke_width=stroke, stroke_fill=shadow_fill)
        if shadow_blur:
            shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
        image = Image.alpha_composite(image, shadow)
        draw = ImageDraw.Draw(image)

    fill = _rgba(layer["color"])
    stroke_fill = _rgba(layer["stroke_color"])
    for left, line_top, line in placements:
        draw.text((left, line_top), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def build_text_overlay_assets(
    project_dir: Path,
    composition: dict[str, Any],
    canvas_width: int,
    canvas_height: int,
    *,
    window_start_seconds: float = 0,
    window_end_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rasterize normalized layers and return VideoCompose overlay specs."""
    normalized = normalize_text_overlay_composition(composition)
    if window_end_seconds is None:
        layers = [dict(item, _order=index) for index, item in enumerate(normalized["layers"])]
    else:
        layers = composition_layers_for_window(normalized, window_start_seconds, window_end_seconds)
    scale = min(canvas_width, canvas_height) / 1080.0
    output_dir = project_dir / "renders" / "overlays" / "text-layers"
    overlays: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for layer in sorted(layers, key=lambda item: (item["z_index"], item.get("_order", 0))):
        pixel_width = max(1, round(canvas_width * layer["width"]))
        pixel_height = max(1, round(canvas_height * layer["height"]))
        visual = {key: value for key, value in layer.items() if key not in {
            "start_seconds", "end_seconds", "x", "y", "width", "height", "z_index", "locked", "_order",
            "enter_animation", "enter_duration_seconds", "exit_animation", "exit_duration_seconds",
        }}
        fingerprint = hashlib.sha256(json.dumps({"visual": visual, "size": [pixel_width, pixel_height]}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "-", layer["id"])[:48]
        asset_path = output_dir / f"{safe_id}-{fingerprint}.png"
        if not asset_path.is_file():
            _draw_text_asset(asset_path, layer, pixel_width, pixel_height, scale)
        overlay = {
            "asset_path": str(asset_path),
            "start_seconds": layer["start_seconds"],
            "end_seconds": layer["end_seconds"],
            "x": round(canvas_width * layer["x"]),
            "y": round(canvas_height * layer["y"]),
            "width": pixel_width,
            "height": pixel_height,
            "shape": "rectangle",
            "text_layer_id": layer["id"],
            "z_index": layer["z_index"],
            "enter_animation": layer["enter_animation"],
            "enter_duration_seconds": layer["enter_duration_seconds"],
            "exit_animation": layer["exit_animation"],
            "exit_duration_seconds": layer["exit_duration_seconds"],
        }
        overlays.append(overlay)
        assets.append({
            "layer_id": layer["id"], "path": str(asset_path), "pixel_rect": {
                "x": overlay["x"], "y": overlay["y"], "width": pixel_width, "height": pixel_height,
            },
        })
    report = {
        "version": normalized["version"],
        "revision": normalized["revision"],
        "canvas": {"width": canvas_width, "height": canvas_height},
        "time_window": {"start_seconds": window_start_seconds, "end_seconds": window_end_seconds},
        "layers": deepcopy(layers),
        "assets": assets,
        "safe_zone": {"inset": TEXT_OVERLAY_SAFE_INSET, "warnings": safe_zone_warnings(normalized)},
    }
    return overlays, report
