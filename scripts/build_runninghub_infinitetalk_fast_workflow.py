"""Build a 24GB speed-first InfiniteTalk project workflow.

This preserves the downloaded community workflow as an immutable source and
emits a separate RunningHub/ComfyUI project JSON.  It deliberately keeps the
original model stack while trading VRAM for speed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "runninghub"
SOURCE = CONFIG / "infinitetalk_source_original_0016e65b.json"
EXPECTED_SOURCE_SHA256 = "0016e65bf817b2887576ca9bbc549ff29a84a33c166ec5d4641e10154d9fd2ec"
MAX_RUNNINGHUB_FILENAME_CHARS = 50

REMOVED_DEBUG_NODES = {5, 40, 42}

POSITIVE_PROMPT = (
    "一位专业女性主持人坐在录音间，正对镜头自然说话。镜头固定，保持人物身份、"
    "五官、发型、服装、双手、麦克风和录音室背景一致。只允许轻微自然的头部动作、"
    "自然眨眼和克制的手部动作。不要唱歌，不要跳舞，不要挥手，不要比心，不要飞吻，"
    "不要移动镜头，不要切换场景，不要出现其他人物。"
)

NEGATIVE_PROMPT = (
    "唱歌，跳舞，挥手，比心，飞吻，夸张动作，大幅度手势，镜头移动，镜头缩放，"
    "场景切换，身份变化，发型变化，服装变化，背景变化，额外人物，字幕，文字，水印，"
    "过度曝光，模糊，低质量，JPEG压缩残留，面部变形，五官错位，多余手指，手部畸形，"
    "身体畸形，闪烁，抽搐，静止画面。"
)

RECOMMENDED_POSITIVE_PROMPT = (
    "一位专业女性主持人坐在录音间，以中近景正对镜头自然说话。保持人物身份、"
    "五官、发型、服装、麦克风和录音室背景一致。脸部清晰，嘴唇随音频自然开合，"
    "只允许轻微自然的头部动作、自然眨眼和克制的手部动作。不要唱歌，不要跳舞，"
    "不要挥手，不要比心，不要飞吻，不要移动镜头，不要切换场景，不要出现其他人物。"
)


@dataclass(frozen=True)
class RenderProfile:
    name: str
    target: Path
    manifest: Path
    width: int
    height: int
    blocks_to_swap: int
    offload_img_emb: bool
    offload_txt_emb: bool
    positive_prompt: str
    input_title: str
    output_prefix: str

    @property
    def dimensions(self) -> str:
        return f"{self.width}×{self.height}"


FAST_PROFILE = RenderProfile(
    name="InfiniteTalk Standard24 Fast 256x320 4step",
    target=CONFIG / "it24_fast_256x320_4s_v2.json",
    manifest=CONFIG / "it24_fast_256x320_4s_v2.manifest.json",
    width=256,
    height=320,
    blocks_to_swap=8,
    offload_img_emb=False,
    offload_txt_emb=False,
    positive_prompt=POSITIVE_PROMPT,
    input_title="① 上传4:5人物参考图（仅修改这里）",
    output_prefix="InfiniteTalk_STANDARD24_FAST_256x320_4step",
)

RECOMMENDED_PROFILE = RenderProfile(
    name="InfiniteTalk 工作流 384×480推荐档 V2",
    target=CONFIG / "InfiniteTalk 工作流 384×480推荐档 V2.json",
    manifest=CONFIG / "InfiniteTalk 工作流 384×480推荐档 V2.manifest.json",
    width=384,
    height=480,
    # 384×480 is 2.25x the latent area of the speed probe.  Swapping 16
    # blocks plus embedding offload keeps a useful Standard 24GB headroom.
    blocks_to_swap=16,
    offload_img_emb=True,
    offload_txt_emb=True,
    positive_prompt=RECOMMENDED_POSITIVE_PROMPT,
    input_title="① 上传4:5半身近景参考图（脸占画面35%—45%，仅修改这里）",
    output_prefix="InfiniteTalk_384x480_recommended_V2",
)

PROFILES = (FAST_PROFILE, RECOMMENDED_PROFILE)
TARGET = FAST_PROFILE.target
MANIFEST = FAST_PROFILE.manifest
RECOMMENDED_TARGET = RECOMMENDED_PROFILE.target


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


def _node(workflow: dict, node_id: int) -> dict:
    matches = [node for node in workflow["nodes"] if int(node["id"]) == node_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one node {node_id}, found {len(matches)}")
    return matches[0]


def _remove_nodes(workflow: dict, node_ids: set[int]) -> None:
    removed_link_ids = {
        int(link[0])
        for link in workflow["links"]
        if int(link[1]) in node_ids or int(link[3]) in node_ids
    }
    workflow["links"] = [
        link for link in workflow["links"] if int(link[0]) not in removed_link_ids
    ]
    workflow["nodes"] = [
        node for node in workflow["nodes"] if int(node["id"]) not in node_ids
    ]
    for node in workflow["nodes"]:
        for input_slot in node.get("inputs", []):
            if input_slot.get("link") in removed_link_ids:
                input_slot["link"] = None
        for output_slot in node.get("outputs", []):
            links = output_slot.get("links")
            if isinstance(links, list):
                output_slot["links"] = [
                    link_id for link_id in links if link_id not in removed_link_ids
                ]


def build(source: dict, profile: RenderProfile = FAST_PROFILE) -> dict:
    workflow = copy.deepcopy(source)

    image_scale = _node(workflow, 2)
    image_scale["title"] = (
        f"{profile.name}人物图预处理：{profile.dimensions}（4:5，自动，请勿修改）"
    )
    image_scale["widgets_values"][0:3] = ["custom", 4, 5]
    image_scale["widgets_values"][7] = profile.height

    long_edge = _node(workflow, 25)
    long_edge["title"] = (
        f"输出长边：{profile.height}（4:5对应{profile.dimensions}，请勿修改）"
    )
    # LiteGraph project JSON stores single-value widgets as one-element arrays.
    # A scalar imports as zero in RunningHub and causes ImageScale division by zero.
    long_edge["widgets_values"] = [profile.height]

    image_to_video = _node(workflow, 14)
    image_to_video["title"] = (
        f"InfiniteTalk {profile.dimensions}图生视频条件（自动，请勿修改）"
    )
    image_to_video["widgets_values"][0:2] = [profile.width, profile.height]
    image_to_video["widgets_values"][7] = False

    sampler = _node(workflow, 13)
    sampler["title"] = "InfiniteTalk rCM 4步采样（Standard 24GB）"
    sampler["widgets_values"][0] = 4
    sampler["widgets_values"][5] = False

    block_swap = _node(workflow, 33)
    block_swap["title"] = (
        f"24GB推荐档：交换{profile.blocks_to_swap}个Wan块并卸载嵌入（自动，请勿修改）"
    )
    block_swap["widgets_values"] = [
        profile.blocks_to_swap,
        profile.offload_img_emb,
        profile.offload_txt_emb,
        True,
        0,
        1,
        False,
    ]

    output = _node(workflow, 24)
    output["title"] = (
        f"最终输出：{profile.dimensions}、严格按音频时长裁切的MP4（{profile.name}）"
    )
    output["widgets_values"]["filename_prefix"] = profile.output_prefix
    output["widgets_values"]["crf"] = 20
    output["widgets_values"]["trim_to_audio"] = True
    output["widgets_values"]["frame_rate"] = 25

    _node(workflow, 36)["title"] = profile.input_title
    _node(workflow, 34)["title"] = "② 上传10秒内纯净口播音频（仅修改这里）"
    _node(workflow, 20)["title"] = "音频截取：0—10秒（短台词极速版）"
    prompt = _node(workflow, 30)
    prompt["title"] = "自然口播动作提示词（已连接到编码器）"
    prompt["widgets_values"] = [profile.positive_prompt]

    text_encode = _node(workflow, 12)
    text_encode["title"] = "自然口播提示词编码（自动，请勿修改）"
    # Keep the visible internal defaults synchronized with the linked prompt so
    # RunningHub does not misleadingly display the community singing/dancing text.
    text_encode["widgets_values"][0] = profile.positive_prompt
    text_encode["widgets_values"][1] = NEGATIVE_PROMPT
    text_encode["widgets_values"][2] = True
    text_encode["widgets_values"][3] = True
    text_encode["widgets_values"][4] = "gpu"

    _remove_nodes(workflow, REMOVED_DEBUG_NODES)
    workflow.setdefault("extra", {})["openmontage_profile"] = {
        "name": profile.name,
        "source_sha256": EXPECTED_SOURCE_SHA256,
        "input_image_node": 36,
        "input_audio_node": 34,
        "output_node": 24,
        "width": profile.width,
        "height": profile.height,
        "fps": 25,
        "steps": 4,
        "blocks_to_swap": profile.blocks_to_swap,
        "force_offload": False,
    }
    return workflow


def validate(workflow: dict, profile: RenderProfile = FAST_PROFILE) -> None:
    node_ids = [int(node["id"]) for node in workflow["nodes"]]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("Duplicate node IDs")
    known_nodes = set(node_ids)
    known_links = {int(link[0]) for link in workflow["links"]}
    for link in workflow["links"]:
        if int(link[1]) not in known_nodes or int(link[3]) not in known_nodes:
            raise ValueError(f"Dangling graph link: {link}")
    for node in workflow["nodes"]:
        for input_slot in node.get("inputs", []):
            link_id = input_slot.get("link")
            if link_id is not None and int(link_id) not in known_links:
                raise ValueError(f"Node {node['id']} has missing input link {link_id}")
        for output_slot in node.get("outputs", []):
            for link_id in output_slot.get("links") or []:
                if int(link_id) not in known_links:
                    raise ValueError(f"Node {node['id']} has missing output link {link_id}")

    if any(node_id in known_nodes for node_id in REMOVED_DEBUG_NODES):
        raise ValueError("Debug-only nodes were not removed")
    if _node(workflow, 2)["widgets_values"][0:3] != ["custom", 4, 5]:
        raise ValueError("Image preprocessing is not 4:5")
    if _node(workflow, 25)["widgets_values"] != [profile.height]:
        raise ValueError(f"Long edge is not {profile.height}")
    if _node(workflow, 14)["widgets_values"][0:2] != [profile.width, profile.height]:
        raise ValueError(f"Image-to-video dimensions are not {profile.dimensions}")
    if _node(workflow, 13)["widgets_values"][0] != 4:
        raise ValueError("Sampler is not 4 steps")
    if _node(workflow, 13)["widgets_values"][5] is not False:
        raise ValueError("Sampler still forces model offload")
    if _node(workflow, 30)["widgets_values"] != [profile.positive_prompt]:
        raise ValueError("External speaking prompt is incorrect")
    text_encode = _node(workflow, 12)["widgets_values"]
    if text_encode[0] != profile.positive_prompt or text_encode[1] != NEGATIVE_PROMPT:
        raise ValueError("Text encoder prompts are not synchronized")
    if text_encode[2:5] != [True, True, "gpu"]:
        raise ValueError("Text encoder cache/offload policy is incorrect")
    if _node(workflow, 33)["widgets_values"] != [
        profile.blocks_to_swap,
        profile.offload_img_emb,
        profile.offload_txt_emb,
        True,
        0,
        1,
        False,
    ]:
        raise ValueError("24GB block-swap policy is incorrect")
    output = _node(workflow, 24)["widgets_values"]
    if output["trim_to_audio"] is not True or output["frame_rate"] != 25:
        raise ValueError("Final audio/FPS contract is incorrect")


def main() -> None:
    if _sha256(SOURCE) != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("InfiniteTalk source snapshot hash changed")
    source = _load(SOURCE)
    for profile in PROFILES:
        workflow = build(source, profile)
        validate(workflow, profile)
        _write(profile.target, workflow)
        _write(
            profile.manifest,
            {
            "workflow_name": profile.name,
            "workflow_type": "comfyui_project_json",
            "source_path": str(SOURCE.relative_to(ROOT)),
            "source_sha256": EXPECTED_SOURCE_SHA256,
            "project_path": str(profile.target.relative_to(ROOT)),
            "input_nodes": {"image": "36", "audio": "34"},
            "output_node": "24",
            "model_stack": {
                "base": "aniWan2114BFp8E4m3fn_i2v480pNew.safetensors",
                "base_precision": "fp16_fast",
                "infinitetalk": "InfiniteTalk/Wan2_1-InfiniTetalk-Single_fp16.safetensors",
                "vae": "Wan2_1_VAE_bf16.safetensors",
                "text_encoder": "umt5-xxl-enc-bf16.safetensors",
                "wav2vec": "TencentGameMate/chinese-wav2vec2-base",
                "loras": [
                    "Wan_2_1_T2V_14B_rCM_lora_average_rank_83_bf16.safetensors",
                    "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
                ],
            },
            "render": {
                "width": profile.width,
                "height": profile.height,
                "fps": 25,
                "steps": 4,
                "blocks_to_swap": profile.blocks_to_swap,
                "force_offload": False,
                "max_audio_seconds": 10,
            },
            "api_export_required_after_import": True,
            },
        )
    if _sha256(SOURCE) != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("InfiniteTalk source snapshot was modified")
        print(profile.target)
        print(profile.manifest)


if __name__ == "__main__":
    main()
