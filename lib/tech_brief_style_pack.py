"""Versioned, local style-pack contract for OpenMontage tech briefs.

The source Open Design export is intentionally *not* read at render time.
This module only consumes the small, reviewed contract under
``styles/hyperframes/tech-brief-v1`` and turns a workbench scene into a
portable HyperFrames context.  The resulting context is also written into
the HyperFrames workspace, so a rendered candidate remains reproducible even
if a later style pack is introduced.

Captions deliberately stay out of this module's graphic copy: OpenMontage's
subtitle renderer owns phrase timing, text editing and final overlay.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


STYLE_PACK_ID = "tech-brief-v1"
STYLE_PACK_VERSION = "1.0.0"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACK_ROOT = _REPO_ROOT / "styles" / "hyperframes" / STYLE_PACK_ID
_REQUIRED_FILES = (
    "tokens.json",
    "aspect-profiles.json",
    "scene-recipes.json",
    "subtitle-recommendation.json",
    "provenance.json",
    "prompt-policy.md",
    "frame.md",
    "visual-style.md",
)
_DEFAULT_FORBIDDEN = ("second_presenter", "baked_caption", "watermark")


class StylePackError(ValueError):
    """The frozen style-pack contract is missing or malformed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StylePackError(f"无法读取风格包文件 {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StylePackError(f"风格包文件 {path.name} 必须是 JSON 对象")
    return value


@lru_cache(maxsize=4)
def load_style_pack(style_pack_id: str = STYLE_PACK_ID) -> dict[str, Any]:
    """Load and validate a frozen local style pack.

    This validation is intentionally strict.  A corrupt style package should
    block a render with a useful message rather than silently falling back to
    the old generic blue template.
    """
    if style_pack_id != STYLE_PACK_ID:
        raise StylePackError(f"未安装风格包：{style_pack_id}")
    missing = [name for name in _REQUIRED_FILES if not (_PACK_ROOT / name).is_file()]
    if missing:
        raise StylePackError(f"科技快报风格包缺少文件：{', '.join(missing)}")
    tokens = _read_json(_PACK_ROOT / "tokens.json")
    aspects = _read_json(_PACK_ROOT / "aspect-profiles.json")
    recipes = _read_json(_PACK_ROOT / "scene-recipes.json")
    subtitle = _read_json(_PACK_ROOT / "subtitle-recommendation.json")
    provenance = _read_json(_PACK_ROOT / "provenance.json")
    if tokens.get("id") != STYLE_PACK_ID or tokens.get("version") != STYLE_PACK_VERSION:
        raise StylePackError("科技快报风格包标识或版本不匹配")
    if "portrait" not in aspects or not isinstance(aspects["portrait"], dict):
        raise StylePackError("科技快报风格包缺少 portrait 画幅配置")
    if not isinstance(recipes.get("recipes"), dict) or not recipes["recipes"]:
        raise StylePackError("科技快报风格包缺少场景配方")
    for recipe_id, recipe in recipes["recipes"].items():
        if not isinstance(recipe, dict):
            raise StylePackError(f"科技快报风格包配方 {recipe_id} 无效")
        variants = recipe.get("variants")
        if not isinstance(variants, list) or not variants:
            raise StylePackError(f"科技快报风格包配方 {recipe_id} 缺少版式变体")
        variant_ids: set[str] = set()
        for variant in variants:
            if not isinstance(variant, dict):
                raise StylePackError(f"科技快报风格包配方 {recipe_id} 的版式变体无效")
            variant_id = str(variant.get("id") or "")
            motion_id = str(variant.get("motion_variant") or "")
            if not variant_id or not motion_id or variant_id in variant_ids:
                raise StylePackError(f"科技快报风格包配方 {recipe_id} 的版式变体无效")
            variant_ids.add(variant_id)
        if str(recipe.get("default_variant") or "") not in variant_ids:
            raise StylePackError(f"科技快报风格包配方 {recipe_id} 缺少有效默认版式")
    if not isinstance(subtitle.get("style"), dict):
        raise StylePackError("科技快报风格包缺少字幕推荐样式")
    return {
        "id": STYLE_PACK_ID,
        "version": STYLE_PACK_VERSION,
        "root": str(_PACK_ROOT),
        "tokens": tokens,
        "aspects": aspects,
        "recipes": recipes,
        "subtitle": subtitle,
        "provenance": provenance,
    }


def style_pack_summary(style_pack_id: str = STYLE_PACK_ID) -> dict[str, Any]:
    """Return the small, browser-safe summary exposed by the workbench."""
    pack = load_style_pack(style_pack_id)
    portrait = pack["aspects"]["portrait"]
    return {
        "id": pack["id"],
        "version": pack["version"],
        "name": str(pack["tokens"].get("name") or "科技快报风格包 V1"),
        "portrait_status": str(portrait.get("status") or "unknown"),
        "landscape_status": str((pack["aspects"].get("landscape") or {}).get("status") or "not-configured"),
        "recipes": [
            {
                "id": key,
                "name": str(value.get("name") or key),
                "description": str(value.get("description") or ""),
                "default_variant": str(value.get("default_variant") or ""),
                "variants": [
                    {
                        "id": str(variant.get("id") or ""),
                        "name": str(variant.get("name") or variant.get("id") or ""),
                        "description": str(variant.get("description") or ""),
                        "motion_variant": str(variant.get("motion_variant") or ""),
                    }
                    for variant in value.get("variants") or []
                    if isinstance(variant, dict) and str(variant.get("id") or "")
                ],
            }
            for key, value in pack["recipes"]["recipes"].items()
            if isinstance(value, dict)
        ],
        "subtitle_template_id": str(pack["subtitle"].get("id") or "subtitle-tech-brief-v1"),
        "caption_is_separate": bool((pack["subtitle"].get("rules") or {}).get("caption_is_never_baked_into_visual", True)),
    }


def recommended_subtitle_style(style_pack_id: str = STYLE_PACK_ID) -> dict[str, Any]:
    """Return a copy so callers cannot mutate the cached package."""
    return deepcopy(load_style_pack(style_pack_id)["subtitle"]["style"])


def layout_variant_catalog(style_pack_id: str = STYLE_PACK_ID) -> dict[str, list[dict[str, str]]]:
    """Return browser-safe layout choices grouped by semantic recipe.

    The frozen pack is the source of truth.  Callers should never invent a
    visual variant at runtime: a missing or malformed choice is resolved by
    :func:`resolve_layout_variant` to the recipe's compatibility default.
    """
    pack = load_style_pack(style_pack_id)
    result: dict[str, list[dict[str, str]]] = {}
    for recipe_id, recipe in pack["recipes"]["recipes"].items():
        if not isinstance(recipe, dict):
            continue
        result[str(recipe_id)] = [
            {
                "id": str(variant.get("id") or ""),
                "name": str(variant.get("name") or variant.get("id") or ""),
                "description": str(variant.get("description") or ""),
                "motion_variant": str(variant.get("motion_variant") or ""),
            }
            for variant in recipe.get("variants") or []
            if isinstance(variant, dict) and str(variant.get("id") or "")
        ]
    return result


def resolve_layout_variant(
    recipe_id: str,
    requested_variant: object = None,
    *,
    style_pack_id: str = STYLE_PACK_ID,
) -> dict[str, str]:
    """Return one valid layout entry, falling back to the frozen default.

    This gives historical plans a stable compatibility path: plans created
    before layout variants existed do not change their visual geometry.
    """
    pack = load_style_pack(style_pack_id)
    recipes = pack["recipes"]["recipes"]
    recipe_key = str(recipe_id or pack["recipes"].get("default_recipe") or "relationship_map")
    if recipe_key not in recipes:
        recipe_key = str(pack["recipes"].get("default_recipe") or "relationship_map")
    recipe = recipes[recipe_key]
    variants = layout_variant_catalog(style_pack_id).get(recipe_key) or []
    requested = str(requested_variant or "")
    selected = next((item for item in variants if item["id"] == requested), None)
    if selected is None:
        default_id = str(recipe.get("default_variant") or "")
        selected = next((item for item in variants if item["id"] == default_id), None)
    if selected is None:
        raise StylePackError(f"科技快报风格包配方 {recipe_key} 没有可用版式")
    return {"recipe_id": recipe_key, **deepcopy(selected)}


def style_pack_playbook(style_pack_id: str = STYLE_PACK_ID) -> dict[str, Any]:
    """Adapt local tokens to the generic HyperFrames style bridge input."""
    pack = load_style_pack(style_pack_id)
    colors = pack["tokens"]["colors"]
    typography = pack["tokens"]["typography"]
    return {
        "id": pack["id"],
        "name": "科技快报风格包 V1",
        "visual_language": {
            "color_palette": {
                "background": colors["paper"],
                "text": colors["ink"],
                "accent": colors["orange"],
                "primary": colors["green"],
                "secondary": colors["yellow"],
                "surface": colors["paper_deep"],
                "muted_text": colors["ink_soft"],
            }
        },
        "typography": {
            "heading": {"font": typography["display"]},
            "body": {"font": typography["body"]},
            "code": {"font": typography["mono"]},
        },
        "motion": {"pace": "moderate"},
    }


def _clip(value: object, maximum: int) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return text[:maximum].rstrip("，、。！？；;:： ")


def _scene_source_text(scene: dict[str, Any]) -> str:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    return _clip(narration.get("text") or scene.get("description") or scene.get("shot_intent"), 600)


def _clauses(text: str, maximum: int) -> list[str]:
    values = re.split(r"[。！？；;\n]+|(?<=，)", text)
    result: list[str] = []
    for value in values:
        cleaned = _clip(value, maximum)
        if len(cleaned) < 3 or cleaned in result:
            continue
        result.append(cleaned)
    return result


def _meaningful_headline(candidate: object, scene: dict[str, Any], clauses: list[str], maximum: int) -> str:
    value = _clip(candidate, maximum)
    title = _clip(scene.get("title"), maximum)
    defaultish = re.fullmatch(r"(?:T|场景|scene)[-_ ]*\d+.*", value or title, flags=re.IGNORECASE)
    if value and not defaultish:
        return value
    if len(clauses) > 1:
        return _clip(clauses[1].removeprefix("而是"), maximum)
    if clauses:
        return _clip(clauses[0], maximum)
    return "本段核心信息"


def _nodes(
    components: object,
    clauses: list[str],
    minimum: int,
    maximum: int,
    node_max_chars: int,
) -> list[str]:
    raw = components if isinstance(components, list) else []
    output: list[str] = []
    for value in [*raw, *clauses]:
        cleaned = _clip(value, node_max_chars)
        if cleaned and cleaned not in output:
            output.append(cleaned)
        if len(output) >= maximum:
            break
    fallback = ["形成关系", "进入现实", "产生价值", "保持可审计"]
    for item in fallback:
        if len(output) >= minimum:
            break
        if item not in output:
            output.append(item)
    return output[:maximum]


def build_style_context(
    *,
    scene: dict[str, Any],
    plan: dict[str, Any],
    width: int,
    height: int,
    duration_seconds: float,
    style_pack_id: str = STYLE_PACK_ID,
) -> dict[str, Any]:
    """Build deterministic graphic-copy and safe-zone input for HyperFrames."""
    pack = load_style_pack(style_pack_id)
    aspect_key = "portrait" if height >= width else "landscape"
    aspect = pack["aspects"].get(aspect_key)
    if not isinstance(aspect, dict):
        raise StylePackError(f"风格包未提供 {aspect_key} 画幅配置")
    spec = plan.get("structured_spec") if isinstance(plan.get("structured_spec"), dict) else {}
    requested_recipe = str(spec.get("scene_recipe") or pack["recipes"].get("default_recipe") or "relationship_map")
    recipes = pack["recipes"]["recipes"]
    if requested_recipe not in recipes:
        requested_recipe = str(pack["recipes"].get("default_recipe") or "relationship_map")
    recipe = recipes[requested_recipe]
    variant = resolve_layout_variant(
        requested_recipe,
        spec.get("layout_variant"),
        style_pack_id=style_pack_id,
    )
    source = _scene_source_text(scene)
    clauses = _clauses(source, int(aspect.get("node_max_chars") or 10))
    headline = _meaningful_headline(spec.get("headline"), scene, clauses, int(aspect.get("headline_max_chars") or 18))
    graphic_nodes = _nodes(
        spec.get("components"),
        clauses,
        int(recipe.get("min_nodes") or 0),
        int(recipe.get("max_nodes") or 4),
        int(aspect.get("node_max_chars") or 10),
    )
    speaker = ""
    presenter = scene.get("presenter") if isinstance(scene.get("presenter"), dict) else {}
    speaker = _clip(presenter.get("speaker_name") or presenter.get("role_name") or scene.get("speaker"), 12)
    return {
        "style_pack_id": pack["id"],
        "style_pack_version": pack["version"],
        "aspect_profile": aspect_key,
        "aspect_status": str(aspect.get("status") or "unknown"),
        "scene_id": str(scene.get("id") or "scene"),
        "duration_seconds": round(max(0.5, float(duration_seconds)), 3),
        "render_size": {"width": int(width), "height": int(height)},
        "scene_recipe": requested_recipe,
        "layout_variant": variant["id"],
        "layout_variant_name": variant["name"],
        "motion_variant": variant["motion_variant"],
        "speaker": speaker,
        "spoken_text": source,
        # The renderer receives a small, frozen token snapshot rather than
        # reading an Open Design export at render time. This keeps a
        # candidate reproducible even if a future package is introduced.
        "render_tokens": {
            "colors": deepcopy(pack["tokens"].get("colors") or {}),
            "geometry": deepcopy(pack["tokens"].get("geometry") or {}),
        },
        "graphic_copy": {
            "scene_goal": _clip(spec.get("scene_goal") or headline, 48),
            "headline": headline,
            "supporting_statement": _clip(spec.get("supporting_statement"), 44),
            "eyebrow": _clip(scene.get("title") or scene.get("id") or "科技快报", 20),
            "center_label": _clip(spec.get("center_label") or headline, int(aspect.get("node_max_chars") or 10)),
            "nodes": graphic_nodes,
        },
        "safe_regions": {
            "content": deepcopy(aspect.get("content_region") or {}),
            "presenter": deepcopy(aspect.get("presenter_safe_region") or {}),
            "caption": deepcopy(aspect.get("caption_safe_region") or {}),
        },
        "forbidden": list(_DEFAULT_FORBIDDEN),
        "caption_policy": {
            "owner": "openmontage-subtitle-module",
            "baked_into_hyperframes": False,
            "subtitle_template_id": str(pack["subtitle"].get("id") or "subtitle-tech-brief-v1"),
        },
        "headline_policy": {
            "owner": "openmontage-story-overlay" if spec.get("external_headline") is True else "hyperframes",
            "render_in_hyperframes": spec.get("external_headline") is not True,
        },
    }
