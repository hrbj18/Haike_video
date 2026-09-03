"""Build an independent low-resolution LongCat workflow without touching the source.

The project JSON is the file imported into RunningHub/ComfyUI.  A matching API
workflow is emitted as a convenience for OpenMontage integration tests.  The
generation model, audio timing, FPS, continuation logic, and final audio trim
are intentionally preserved.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "runninghub"
SOURCE_PROJECT = CONFIG / "longcat_avatar_project_fixed.json"
SOURCE_API = CONFIG / "longcat_avatar_api.json"

REMOVED_PREVIEW_NODE_IDS = {292, 311}
MAX_RUNNINGHUB_FILENAME_CHARS = 50

SHORT_VARIANTS = {
    "fast": "fast",
    "standard24_fast": "24fast",
    "standard24_balanced": "24bal",
    "standard24_safe": "safe",
}


@dataclass(frozen=True)
class FastProfile:
    width: int
    height: int
    ratio_width: int
    ratio_height: int
    steps: int = 4
    variant: str = "fast"
    blocks_to_swap: int = 48
    force_offload: bool = True
    offload_img_emb: bool = False
    offload_txt_emb: bool = False
    whisper_load_device: str = "main_device"

    @property
    def dimensions(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def display_dimensions(self) -> str:
        return f"{self.width}×{self.height}"

    @property
    def ratio(self) -> str:
        return f"{self.ratio_width}:{self.ratio_height}"

    @property
    def target_project(self) -> Path:
        return CONFIG / (
            f"lc_{SHORT_VARIANTS[self.variant]}_{self.dimensions}_{self.steps}s.json"
        )

    @property
    def target_api(self) -> Path:
        return CONFIG / (
            f"lc_{SHORT_VARIANTS[self.variant]}_{self.dimensions}_{self.steps}s_api.json"
        )

    @property
    def filename_prefix(self) -> str:
        return (
            f"LongCat_TalkingAvatar_{self.variant.upper()}_"
            f"{self.dimensions}_{self.steps}step"
        )


PROFILES = (
    FastProfile(width=256, height=448, ratio_width=4, ratio_height=7),
    FastProfile(width=256, height=320, ratio_width=4, ratio_height=5),
    FastProfile(
        width=256,
        height=320,
        ratio_width=4,
        ratio_height=5,
        variant="standard24_fast",
        blocks_to_swap=8,
        force_offload=False,
    ),
    FastProfile(
        width=256,
        height=320,
        ratio_width=4,
        ratio_height=5,
        variant="standard24_balanced",
        blocks_to_swap=16,
        force_offload=False,
        offload_img_emb=True,
        offload_txt_emb=True,
    ),
    FastProfile(
        width=256,
        height=320,
        ratio_width=4,
        ratio_height=5,
        variant="standard24_safe",
        # The 15B bf16 model still OOMs on Standard while loading weights when
        # only 24 blocks are swapped.  Preserve the source workflow's proven
        # low-VRAM policy and offload every transformer block for this profile.
        blocks_to_swap=48,
        force_offload=True,
        offload_img_emb=True,
        offload_txt_emb=True,
        whisper_load_device="offload_device",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    if len(path.name) > MAX_RUNNINGHUB_FILENAME_CHARS:
        raise ValueError(
            f"RunningHub filename exceeds {MAX_RUNNINGHUB_FILENAME_CHARS} characters: "
            f"{path.name}"
        )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _project_node(workflow: dict, node_id: int) -> dict:
    matches = [node for node in workflow["nodes"] if int(node["id"]) == node_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one project node {node_id}, found {len(matches)}")
    return matches[0]


def build_project(source: dict, profile: FastProfile) -> dict:
    workflow = copy.deepcopy(source)

    scale = _project_node(workflow, 512)
    scale["title"] = (
        f"极速版人物图预处理：{profile.display_dimensions}"
        f"（{profile.ratio}，自动，请勿修改）"
    )
    scale["widgets_values"][0:3] = [
        "custom",
        profile.ratio_width,
        profile.ratio_height,
    ]
    scale["widgets_values"][7] = profile.height

    long_edge = _project_node(workflow, 554)
    long_edge["title"] = (
        f"输出长边：{profile.height}"
        f"（{profile.ratio}对应{profile.display_dimensions}，请勿修改）"
    )
    # LiteGraph project JSON stores even single-value widgets as an array.
    # Writing a scalar imports as an empty/zero constant in RunningHub.
    long_edge["widgets_values"] = [profile.height]

    steps = _project_node(workflow, 245)
    steps["title"] = f"采样步数：{profile.steps}（极速版，请勿修改）"
    steps["widgets_values"] = [profile.steps]

    scheduler = _project_node(workflow, 537)
    scheduler["title"] = f"{profile.steps}步蒸馏采样调度（极速版，请勿修改）"
    scheduler["widgets_values"][1] = profile.steps

    block_swap = _project_node(workflow, 104)
    if profile.variant == "standard24_fast":
        block_swap["title"] = (
            f"24GB速度优先：仅交换{profile.blocks_to_swap}个LongCat块（自动，请勿修改）"
        )
    elif profile.variant == "standard24_balanced":
        block_swap["title"] = (
            f"24GB平衡版：交换{profile.blocks_to_swap}个LongCat块并卸载嵌入（自动，请勿修改）"
        )
    elif profile.variant == "standard24_safe":
        block_swap["title"] = (
            f"24GB安全版：交换{profile.blocks_to_swap}个LongCat块并卸载嵌入（自动，请勿修改）"
        )
    block_swap["widgets_values"][0] = profile.blocks_to_swap
    block_swap["widgets_values"][1] = profile.offload_img_emb
    block_swap["widgets_values"][2] = profile.offload_txt_emb

    for sampler_id in (536, 538):
        sampler = _project_node(workflow, sampler_id)
        sampler["widgets_values"][3] = profile.force_offload
        if profile.variant == "standard24_fast":
            sampler["title"] = sampler["title"].replace("（自动）", "（24GB驻留加速）")

    whisper = _project_node(workflow, 534)
    whisper["widgets_values"][2] = profile.whisper_load_device
    if profile.variant == "standard24_safe":
        whisper["title"] = "Whisper音频编码器：移出主显存（24GB安全版）"

    output = _project_node(workflow, 352)
    output["title"] = (
        f"最终输出：{profile.display_dimensions}、严格按音频时长裁切的MP4（极速版）"
    )
    output["widgets_values"]["filename_prefix"] = profile.filename_prefix
    # Slightly lighter final encode; inference remains the dominant cost.
    output["widgets_values"]["crf"] = 20

    # These two sink nodes only create additional temporary MP4 previews.  They
    # have no downstream consumers, so removing them avoids duplicate encoding.
    workflow["nodes"] = [
        node for node in workflow["nodes"] if int(node["id"]) not in REMOVED_PREVIEW_NODE_IDS
    ]
    workflow["links"] = [
        link
        for link in workflow["links"]
        if int(link[1]) not in REMOVED_PREVIEW_NODE_IDS
        and int(link[3]) not in REMOVED_PREVIEW_NODE_IDS
    ]
    return workflow


def build_api(source: dict, profile: FastProfile) -> dict:
    workflow = copy.deepcopy(source)

    workflow["512"]["inputs"].update(
        {
            "aspect_ratio": "custom",
            "proportional_width": profile.ratio_width,
            "proportional_height": profile.ratio_height,
        }
    )
    workflow["512"]["_meta"]["title"] = (
        f"极速版人物图预处理：{profile.display_dimensions}"
        f"（{profile.ratio}，自动，请勿修改）"
    )
    workflow["554"]["inputs"]["value"] = profile.height
    workflow["554"]["_meta"]["title"] = (
        f"输出长边：{profile.height}"
        f"（{profile.ratio}对应{profile.display_dimensions}，请勿修改）"
    )
    workflow["245"]["inputs"]["value"] = profile.steps
    workflow["245"]["_meta"]["title"] = (
        f"采样步数：{profile.steps}（极速版，请勿修改）"
    )
    workflow["537"]["_meta"]["title"] = (
        f"{profile.steps}步蒸馏采样调度（极速版，请勿修改）"
    )
    workflow["104"]["inputs"]["blocks_to_swap"] = profile.blocks_to_swap
    workflow["104"]["inputs"]["offload_img_emb"] = profile.offload_img_emb
    workflow["104"]["inputs"]["offload_txt_emb"] = profile.offload_txt_emb
    if profile.variant == "standard24_fast":
        workflow["104"]["_meta"]["title"] = (
            f"24GB速度优先：仅交换{profile.blocks_to_swap}个LongCat块（自动，请勿修改）"
        )
    elif profile.variant == "standard24_balanced":
        workflow["104"]["_meta"]["title"] = (
            f"24GB平衡版：交换{profile.blocks_to_swap}个LongCat块并卸载嵌入（自动，请勿修改）"
        )
    elif profile.variant == "standard24_safe":
        workflow["104"]["_meta"]["title"] = (
            f"24GB安全版：交换{profile.blocks_to_swap}个LongCat块并卸载嵌入（自动，请勿修改）"
        )
    for sampler_id in ("536", "538"):
        workflow[sampler_id]["inputs"]["force_offload"] = profile.force_offload
        if profile.variant == "standard24_fast":
            workflow[sampler_id]["_meta"]["title"] = workflow[sampler_id]["_meta"][
                "title"
            ].replace("（自动）", "（24GB驻留加速）")
    workflow["534"]["inputs"]["load_device"] = profile.whisper_load_device
    if profile.variant == "standard24_safe":
        workflow["534"]["_meta"]["title"] = (
            "Whisper音频编码器：移出主显存（24GB安全版）"
        )
    workflow["352"]["inputs"]["filename_prefix"] = profile.filename_prefix
    workflow["352"]["inputs"]["crf"] = 20
    workflow["352"]["_meta"]["title"] = (
        f"最终输出：{profile.display_dimensions}、严格按音频时长裁切的MP4（极速版）"
    )

    for node_id in REMOVED_PREVIEW_NODE_IDS:
        workflow.pop(str(node_id), None)
    return workflow


def validate_project(workflow: dict, profile: FastProfile) -> None:
    ids = [int(node["id"]) for node in workflow["nodes"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate project node IDs")
    known = set(ids)
    for link in workflow["links"]:
        if int(link[1]) not in known or int(link[3]) not in known:
            raise ValueError(f"Dangling project link: {link}")
    if any(node_id in known for node_id in REMOVED_PREVIEW_NODE_IDS):
        raise ValueError("Redundant preview output nodes were not removed")
    expected_ratio = ["custom", profile.ratio_width, profile.ratio_height]
    if _project_node(workflow, 512)["widgets_values"][0:3] != expected_ratio:
        raise ValueError(f"Project aspect ratio is not {profile.ratio}")
    if _project_node(workflow, 554)["widgets_values"] != [profile.height]:
        raise ValueError(f"Project long edge is not {profile.height}")
    if _project_node(workflow, 245)["widgets_values"] != [profile.steps]:
        raise ValueError(f"Project step count is not {profile.steps}")
    if _project_node(workflow, 104)["widgets_values"][0] != profile.blocks_to_swap:
        raise ValueError(f"Project block swap is not {profile.blocks_to_swap}")
    if _project_node(workflow, 104)["widgets_values"][1:3] != [
        profile.offload_img_emb,
        profile.offload_txt_emb,
    ]:
        raise ValueError("Project embedding offload policy is incorrect")
    for sampler_id in (536, 538):
        if _project_node(workflow, sampler_id)["widgets_values"][3] is not profile.force_offload:
            raise ValueError(
                f"Project sampler {sampler_id} force_offload is not {profile.force_offload}"
            )
    if _project_node(workflow, 534)["widgets_values"][2] != profile.whisper_load_device:
        raise ValueError("Project Whisper load device is incorrect")
    if _project_node(workflow, 352)["widgets_values"]["trim_to_audio"] is not True:
        raise ValueError("Final output must remain trimmed to source audio")


def validate_api(workflow: dict, profile: FastProfile) -> None:
    known = set(workflow)
    for node_id, node in workflow.items():
        for value in node.get("inputs", {}).values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0].isdigit()
                and value[0] not in known
            ):
                raise ValueError(f"API node {node_id} references missing node {value[0]}")
    if workflow["512"]["inputs"]["aspect_ratio"] != "custom":
        raise ValueError("API aspect ratio is not custom")
    if (
        workflow["512"]["inputs"]["proportional_width"],
        workflow["512"]["inputs"]["proportional_height"],
        workflow["554"]["inputs"]["value"],
    ) != (profile.ratio_width, profile.ratio_height, profile.height):
        raise ValueError(f"API dimensions are not configured for {profile.dimensions}")
    if workflow["245"]["inputs"]["value"] != profile.steps:
        raise ValueError(f"API step count is not {profile.steps}")
    if workflow["104"]["inputs"]["blocks_to_swap"] != profile.blocks_to_swap:
        raise ValueError(f"API block swap is not {profile.blocks_to_swap}")
    if (
        workflow["104"]["inputs"]["offload_img_emb"],
        workflow["104"]["inputs"]["offload_txt_emb"],
    ) != (profile.offload_img_emb, profile.offload_txt_emb):
        raise ValueError("API embedding offload policy is incorrect")
    for sampler_id in ("536", "538"):
        if workflow[sampler_id]["inputs"]["force_offload"] is not profile.force_offload:
            raise ValueError(
                f"API sampler {sampler_id} force_offload is not {profile.force_offload}"
            )
    if workflow["534"]["inputs"]["load_device"] != profile.whisper_load_device:
        raise ValueError("API Whisper load device is incorrect")
    if workflow["352"]["inputs"]["trim_to_audio"] is not True:
        raise ValueError("API output must remain trimmed to source audio")


def main() -> None:
    source_hashes = {_path: _sha256(_path) for _path in (SOURCE_PROJECT, SOURCE_API)}
    source_project = _load(SOURCE_PROJECT)
    source_api = _load(SOURCE_API)
    outputs: list[Path] = []
    for profile in PROFILES:
        project = build_project(source_project, profile)
        api = build_api(source_api, profile)
        validate_project(project, profile)
        validate_api(api, profile)
        _write(profile.target_project, project)
        _write(profile.target_api, api)
        outputs.extend((profile.target_project, profile.target_api))
    for path, digest in source_hashes.items():
        if _sha256(path) != digest:
            raise RuntimeError(f"Source workflow changed unexpectedly: {path}")
    for output in outputs:
        print(output)


if __name__ == "__main__":
    main()
