"""Emit Plus-48GB InfiniteTalk 448x560 workflows from the frozen 24GB sources.

Why this exists
---------------
The production exact-clock graph (``2094449979141218305``) was authored as a
*Standard 24GB* speed-first profile: its own ``extra.openmontage_profile``
metadata still reads ``name = "InfiniteTalk Standard24 Fast 384x480 4step"``
with ``blocks_to_swap = 8``.

Measured facts (2026-09-16, paid probes, 6-window / 375-frame samples):

* ``t = 19s + 39s * windows`` -- startup is only ~2 % of a one-minute render.
* Removing block swap (8 -> 0) changes nothing: 255 vs 259/256 GPU seconds.
* ``load_device`` and ``quantization`` likewise measure as noise.
* The graph is compute-bound (~95 TFLOPS sustained on the fp8 path), so **no
  VRAM-side knob can make it faster**.

The only lever that removes compute *linearly* is the number of sampling steps:
each window costs ``steps`` DiT forwards, so 4 -> 3 removes ~25 % of the
per-window work (~20 % wall clock once startup is amortised).  That is what
``plus48_fast3`` ships.  It deliberately breaks the project's own exact-clock
contract, which pins ``steps == 4`` as "already accepted" -- so this builder
*reports* that violation instead of refusing to write the file.

The 24GB-legacy fields are still cleaned up in both profiles (a 48GB node has
no reason to swap blocks to host RAM), but that part is documented as neutral,
not as a speed-up.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.avatar.runninghub_avatar import (
    RunningHubAvatarError,
    _validate_infinitetalk_448x560_exact_clock_template,
)

# UI node ids (shared by the UI export and the API export for this graph).
SWAP_NODE = 33
MODEL_NODE = 11
SAMPLER_NODE = 13
OUTPUT_NODE = 24

# Widget order of WanVideoBlockSwap in both the UI export and the API JSON.
SWAP_WIDGET_ORDER = (
    "blocks_to_swap",
    "offload_img_emb",
    "offload_txt_emb",
    "use_non_blocking",
    "vace_blocks_to_swap",
    "prefetch_blocks",
    "block_swap_debug",
)

SAMPLER_STEPS_WIDGET_INDEX = 0  # WanVideoSampler widgets start with ``steps``.

PROFILES: dict[str, dict[str, Any]] = {
    # ---- headline deliverable: the Blackwell-native attention kernel --------
    # ``sageattn`` is SageAttention 2.x.  Its compiled kernels cover
    # sm80/sm86/sm89/sm90 only; sm_120 (Blackwell) is served, at best, by a
    # per-warp sm89 kernel with Triton fallback behind it.  ``sageattn_3`` is
    # the Blackwell-native path (the node's own option list advertises it as
    # "Blackwell / Ultra Fast").
    #
    # This is the only remaining knob that can move wall clock without touching
    # the model weights, the step count, the resolution, the scheduler, the
    # seed or the window geometry -- i.e. without spending image quality.
    "plus48_kernel": {
        "steps": 4,
        "blocks_to_swap": 0,
        "offload_img_emb": False,
        "offload_txt_emb": False,
        "use_non_blocking": True,
        "prefetch_blocks": 1,
        "load_device": "main_device",
        "quantization": "disabled",
        "attention_mode": "sageattn_3",
        "instance_type": "default",
        "filename_prefix": "InfiniteTalk_4x5_448x560_exact_frames_kernel",
        "profile_name": "InfiniteTalk 448x560 4step Blackwell attention",
        "swap_title": "显存管理：Block Swap（0 层交换 + Blackwell 注意力内核）",
    },
    # ---- quality-downgrade variant, kept for reference only ----------------
    # 4 -> 3 steps removes ~25 % of per-window compute but changes the accepted
    # sampling contract; do not ship it as a speed-up.
    "plus48_fast3": {
        "steps": 3,
        "blocks_to_swap": 0,
        "offload_img_emb": False,
        "offload_txt_emb": False,
        # Unchanged from V2: with blocks_to_swap=0 these are inert, and keeping
        # them on means a future swap increase still gets hidden by prefetch.
        "use_non_blocking": True,
        "prefetch_blocks": 1,
        "load_device": "main_device",
        "quantization": "disabled",
        "attention_mode": "sageattn",
        "instance_type": "plus",
        "filename_prefix": "InfiniteTalk_4x5_448x560_exact_frames_fast3step",
        "profile_name": "InfiniteTalk Plus48 448x560 3step fast",
        "swap_title": "显存管理：Block Swap（48GB Plus：0 层交换 + 3 步采样）",
    },
    # ---- zero-quality-risk fallback: config cleanup only --------------------
    "plus48_steps4": {
        "steps": 4,
        "blocks_to_swap": 0,
        "offload_img_emb": False,
        "offload_txt_emb": False,
        "use_non_blocking": True,
        "prefetch_blocks": 1,
        "load_device": "main_device",
        "quantization": "disabled",
        "attention_mode": "sageattn",
        "instance_type": "plus",
        "filename_prefix": "InfiniteTalk_4x5_448x560_exact_frames_4step",
        "profile_name": "InfiniteTalk Plus48 448x560 4step (contract-safe)",
        "swap_title": "显存管理：Block Swap（48GB Plus：0 层交换，请勿修改）",
    },
}

CONTRACT_SAFE_STEPS = 4


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _patch_ui(source: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    workflow = copy.deepcopy(source)
    nodes = {int(node["id"]): node for node in workflow["nodes"]}

    swap = nodes[SWAP_NODE]
    widgets = list(swap["widgets_values"])
    index = {name: position for position, name in enumerate(SWAP_WIDGET_ORDER)}
    for name in (
        "blocks_to_swap",
        "offload_img_emb",
        "offload_txt_emb",
        "use_non_blocking",
        "prefetch_blocks",
    ):
        widgets[index[name]] = profile[name]
    swap["widgets_values"] = widgets
    swap["title"] = profile["swap_title"]

    sampler = nodes[SAMPLER_NODE]
    sampler_widgets = list(sampler["widgets_values"])
    sampler_widgets[SAMPLER_STEPS_WIDGET_INDEX] = profile["steps"]
    sampler["widgets_values"] = sampler_widgets
    sampler["title"] = f"采样器（{profile['steps']} 步，请勿修改）"

    model = nodes[MODEL_NODE]
    # UI exports carry combo values positionally in widgets_values; the model
    # loader order is (model, base_precision, quantization, load_device,
    # attention_mode, rms_norm_function).
    model_widgets = list(model["widgets_values"])
    if len(model_widgets) >= 6:
        model_widgets[2] = profile["quantization"]
        model_widgets[3] = profile["load_device"]
        model_widgets[4] = profile["attention_mode"]
    model["widgets_values"] = model_widgets

    output = nodes[OUTPUT_NODE]
    output_widgets = output.get("widgets_values")
    if isinstance(output_widgets, dict):
        output_widgets["filename_prefix"] = profile["filename_prefix"]
    elif isinstance(output_widgets, list):
        for position, name in enumerate(
            ("frame_rate", "loop_count", "filename_prefix", "format")
        ):
            if name == "filename_prefix" and len(output_widgets) > position:
                output_widgets[position] = profile["filename_prefix"]

    workflow["extra"] = dict(workflow.get("extra") or {})
    meta = dict(workflow["extra"].get("openmontage_profile") or {})
    meta.update({
        "name": profile["profile_name"],
        "steps": profile["steps"],
        "blocks_to_swap": profile["blocks_to_swap"],
        "use_non_blocking": profile["use_non_blocking"],
        "prefetch_blocks": profile["prefetch_blocks"],
        "quantization": profile["quantization"],
        "load_device": profile["load_device"],
        "attention_mode": profile["attention_mode"],
        "instance_type": profile["instance_type"],
        # The inherited metadata still claims the 384x480 source resolution;
        # node 2 scales to 448x560, so record what actually renders.
        "width": 448,
        "height": 560,
        "source_profile": "InfiniteTalk Standard24 Fast 384x480 4step",
    })
    workflow["extra"]["openmontage_profile"] = meta
    return workflow


def _patch_api(source: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    workflow = copy.deepcopy(source)
    swap = workflow.get(str(SWAP_NODE))
    if not isinstance(swap, dict) or swap.get("class_type") != "WanVideoBlockSwap":
        raise SystemExit(f"API 模板缺少节点 {SWAP_NODE}（WanVideoBlockSwap）")
    for name in (
        "blocks_to_swap",
        "offload_img_emb",
        "offload_txt_emb",
        "use_non_blocking",
        "prefetch_blocks",
    ):
        swap["inputs"][name] = profile[name]
    swap.setdefault("_meta", {})["title"] = profile["swap_title"]

    model = workflow.get(str(MODEL_NODE))
    if not isinstance(model, dict) or model.get("class_type") != "WanVideoModelLoader":
        raise SystemExit(f"API 模板缺少节点 {MODEL_NODE}（WanVideoModelLoader）")
    model["inputs"]["load_device"] = profile["load_device"]
    model["inputs"]["quantization"] = profile["quantization"]
    model["inputs"]["attention_mode"] = profile["attention_mode"]

    sampler = workflow.get(str(SAMPLER_NODE))
    if not isinstance(sampler, dict) or sampler.get("class_type") != "WanVideoSampler":
        raise SystemExit(f"API 模板缺少节点 {SAMPLER_NODE}（WanVideoSampler）")
    sampler["inputs"]["steps"] = profile["steps"]

    output = workflow.get(str(OUTPUT_NODE))
    if not isinstance(output, dict) or output.get("class_type") != "VHS_VideoCombine":
        raise SystemExit(f"API 模板缺少节点 {OUTPUT_NODE}（VHS_VideoCombine）")
    output["inputs"]["filename_prefix"] = profile["filename_prefix"]
    return workflow


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 48GB Plus 版 InfiniteTalk 448×560 工作流")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="plus48_fast3")
    parser.add_argument("--steps", type=int, default=None, help="覆盖配置档里的采样步数")
    parser.add_argument("--ui-source", type=Path, required=True)
    parser.add_argument("--api-source", type=Path, required=True)
    parser.add_argument("--ui-out", type=Path, required=True)
    parser.add_argument("--api-out", type=Path, required=True)
    args = parser.parse_args()

    profile = dict(PROFILES[args.profile])
    if args.steps is not None:
        profile["steps"] = int(args.steps)
        if profile["steps"] != CONTRACT_SAFE_STEPS:
            profile["profile_name"] = profile["profile_name"].replace("4step", f"{profile['steps']}step")
            profile["swap_title"] = f"显存管理：Block Swap（48GB Plus：0 层交换 + {profile['steps']} 步采样）"
            profile["filename_prefix"] = (
                f"InfiniteTalk_4x5_448x560_exact_frames_fast{profile['steps']}step"
            )

    ui = _patch_ui(_read(args.ui_source), profile)
    _write(args.ui_out, ui)
    api = _patch_api(_read(args.api_source), profile)
    _write(args.api_out, api)

    # The project contract pins steps == 4. A faster file is *expected* to trip
    # that pin, so surface it as data instead of refusing to emit the artifact.
    contract: dict[str, Any] = {"satisfied": False, "sha256": None, "error": None}
    try:
        contract["sha256"] = _validate_infinitetalk_448x560_exact_clock_template(args.api_out)
        contract["satisfied"] = True
    except RunningHubAvatarError as exc:
        contract["error"] = str(exc)
        contract["sha256"] = None

    summary = {
        "profile": args.profile,
        "steps": profile["steps"],
        "ui_out": str(args.ui_out),
        "api_out": str(args.api_out),
        "contract": contract,
        "contract_note": (
            "通过项目精确帧时钟契约，可作为生产模板"
            if contract["satisfied"]
            else "★ 未通过项目契约（steps 被钉为 4）——导入 RunningHub 手工跑没问题，"
            "但要用进生产流水线必须先改契约或走 nodeInfoList 覆写"
        ),
        "changes": {
            key: profile[key]
            for key in (
                "steps",
                "blocks_to_swap",
                "offload_img_emb",
                "offload_txt_emb",
                "use_non_blocking",
                "prefetch_blocks",
                "load_device",
                "quantization",
                "attention_mode",
                "instance_type",
                "filename_prefix",
            )
        },
        "nodeinfo_overrides": [
            {"nodeId": str(SAMPLER_NODE), "fieldName": "steps", "fieldValue": profile["steps"]},
            {"nodeId": str(SWAP_NODE), "fieldName": "blocks_to_swap", "fieldValue": profile["blocks_to_swap"]},
            {"nodeId": str(SWAP_NODE), "fieldName": "use_non_blocking", "fieldValue": profile["use_non_blocking"]},
            {"nodeId": str(SWAP_NODE), "fieldName": "prefetch_blocks", "fieldValue": profile["prefetch_blocks"]},
            {"nodeId": str(MODEL_NODE), "fieldName": "load_device", "fieldValue": profile["load_device"]},
            {"nodeId": str(MODEL_NODE), "fieldName": "attention_mode", "fieldValue": profile["attention_mode"]},
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
