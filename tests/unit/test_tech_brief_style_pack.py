from __future__ import annotations

from lib.tech_brief_style_pack import (
    STYLE_PACK_ID,
    build_style_context,
    layout_variant_catalog,
    load_style_pack,
    recommended_subtitle_style,
    resolve_layout_variant,
    style_pack_summary,
)


def _scene() -> dict:
    return {
        "id": "T003",
        "title": "T003 · 雅雅",
        "description": "重点不是他们又演了什么，而是一个不存在于现实中的数字人，已经可以像真人演员一样积累粉丝、参与宣传并产生商业价值。",
        "presenter": {"speaker_name": "雅雅", "treatment": "pip_top_left"},
    }


def _plan() -> dict:
    return {
        "structured_spec": {
            "headline": "数字人开始影响现实",
            "center_label": "商业角色",
            "components": ["固定形象", "粉丝关系", "宣传参与", "商业价值"],
            "scene_recipe": "relationship_map",
        }
    }


def test_pack_is_complete_and_exposes_a_portrait_contract():
    pack = load_style_pack()
    assert pack["id"] == STYLE_PACK_ID
    assert pack["aspects"]["portrait"]["status"] == "production-ready"
    assert pack["subtitle"]["rules"]["caption_is_never_baked_into_visual"] is True


def test_portrait_context_keeps_captions_out_of_graphic_copy():
    context = build_style_context(
        scene=_scene(), plan=_plan(), width=1080, height=1920, duration_seconds=8
    )
    assert context["aspect_profile"] == "portrait"
    assert context["scene_recipe"] == "relationship_map"
    assert context["caption_policy"]["baked_into_hyperframes"] is False
    assert context["graphic_copy"]["headline"] == "数字人开始影响现实"
    assert context["graphic_copy"]["center_label"] == "商业角色"
    assert context["graphic_copy"]["nodes"] == ["固定形象", "粉丝关系", "宣传参与", "商业价值"]
    assert "spoken_text" not in context["graphic_copy"]
    assert context["render_tokens"]["colors"]["orange"] == "#C87434"


def test_story_scene_reserves_headline_for_openmontage_overlay():
    scene = _scene()
    scene["story_id"] = "S01"

    context = build_style_context(
        scene=scene, plan=_plan(), width=1080, height=1920, duration_seconds=8
    )

    assert context["headline_policy"] == {
        "owner": "openmontage-story-overlay",
        "render_in_hyperframes": False,
    }


def test_layout_variants_are_frozen_and_old_plans_keep_the_default_geometry():
    catalog = layout_variant_catalog()
    assert {item["id"] for item in catalog["relationship_map"]} == {"radial_map", "causal_chain", "convergence"}
    assert resolve_layout_variant("relationship_map")["id"] == "radial_map"
    assert resolve_layout_variant("relationship_map", "not-a-layout")["id"] == "radial_map"

    plan = _plan()
    plan["structured_spec"]["layout_variant"] = "causal_chain"
    context = build_style_context(scene=_scene(), plan=plan, width=1080, height=1920, duration_seconds=8)
    assert context["layout_variant"] == "causal_chain"
    assert context["motion_variant"] == "step_through"


def test_landscape_has_a_non_stretched_compatibility_contract():
    context = build_style_context(
        scene=_scene(), plan=_plan(), width=1920, height=1080, duration_seconds=8
    )
    assert context["aspect_profile"] == "landscape"
    assert context["aspect_status"] == "compatibility-only"


def test_style_summary_and_subtitle_recommendation_are_browser_safe():
    summary = style_pack_summary()
    style = recommended_subtitle_style()
    assert summary["id"] == STYLE_PACK_ID
    relationship = next(recipe for recipe in summary["recipes"] if recipe["id"] == "relationship_map")
    assert relationship["default_variant"] == "radial_map"
    assert any(item["id"] == "causal_chain" for item in relationship["variants"])
    assert style["position"]["anchor"] == "bottom-center"
    assert style["max_lines"] == 2
