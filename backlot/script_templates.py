"""Read-only, auditable script templates for the avatar workbench.

Templates live with the episode source documents rather than inside a project.
Selecting a template only parses and previews it; project files are written by
``workbench.import_avatar_script_template`` after an explicit confirmation.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


TEMPLATE_ROOT = REPO_ROOT / "content" / "episodes"
PUBLIC_TEMPLATE_ROOT = REPO_ROOT / "content" / "templates" / "avatar"
TURN_RE = re.compile(r"^T(?P<number>\d{1,6})$", re.IGNORECASE)
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")
SPEAKER_LINE_RE = re.compile(
    r"^(?:(?P<turn>T\d{1,6})\s*(?:[·•、.\-—]+\s*)?)?(?P<speaker>[^：:]{1,48})[：:]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)
KNOWN_SPEAKER_IDS = {
    "雅雅": "yaya",
    "檬檬": "mengmeng",
    "萌萌": "mengmeng",
    "主持人": "host",
    "旁白": "narrator",
}
NON_DIALOGUE_LABELS = {"录制规则", "使用说明", "导入说明", "注意事项", "提示", "规则"}
SPEAKER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class ScriptTemplateError(ValueError):
    """A template is missing, unsafe, or cannot become a production script."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_markdown_line(value: str) -> str:
    line = value.strip()
    match = re.match(r"^\*\*(?P<label>.+?[：:])\*\*(?P<text>.*)$", line)
    if match:
        line = f"{match.group('label')}{match.group('text')}"
    return line.strip().strip("- ")


def _dialogue_lines(markdown: str) -> tuple[list[tuple[int, str]], list[str]]:
    """Return the most likely dialogue block and parser warnings.

    The episode documents deliberately separate a complete dialogue section
    from per-character clean copy and publication text.  We only accept the
    explicit dialogue block so the same line is never imported twice.
    """
    rows = markdown.splitlines()
    start = None
    level = 2
    for index, row in enumerate(rows):
        heading = HEADING_RE.match(row.strip())
        if heading and re.search(r"(完整.*(?:对话|台词|口播)|(?:对话|台词|口播).*(?:完整|双人|多人))", heading.group("title")):
            start, level = index + 1, len(heading.group("level"))
            break
    if start is None:
        return list(enumerate(rows, 1)), ["未找到“完整对话/台词”标题，已尝试从全文识别说话人台词。"]
    collected: list[tuple[int, str]] = []
    for index in range(start, len(rows)):
        heading = HEADING_RE.match(rows[index].strip())
        if heading and len(heading.group("level")) <= level:
            break
        collected.append((index + 1, rows[index]))
    return collected, []


def _speaker_id(name: str, order: int) -> str:
    cleaned = re.sub(r"\s+", "", name).strip("* _")
    if cleaned in KNOWN_SPEAKER_IDS:
        return KNOWN_SPEAKER_IDS[cleaned]
    ascii_value = re.sub(r"[^a-z0-9_-]+", "-", cleaned.lower()).strip("-")
    if SPEAKER_ID_RE.fullmatch(ascii_value or ""):
        return ascii_value
    return f"speaker-{order}"


def _normalise_turn_id(raw: str | None, index: int) -> str:
    if not raw:
        return f"T{index:03d}"
    match = TURN_RE.fullmatch(raw.strip())
    if not match:
        raise ScriptTemplateError(f"轮次编号不合法：{raw}")
    return f"T{int(match.group('number')):03d}"


def _estimate_seconds(text: str) -> float:
    # This is only a planning estimate. Imported driving audio always replaces
    # it as the timing authority later in the avatar pipeline.
    meaningful = len(re.sub(r"\s+", "", text))
    return round(max(1.5, meaningful / 4.2), 2)


def _parse_template(path: Path) -> dict[str, Any]:
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScriptTemplateError("无法读取模板脚本文档") from exc
    if not markdown.strip():
        raise ScriptTemplateError("模板脚本文档为空")
    rows, warnings = _dialogue_lines(markdown)
    raw_turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_number, raw_line in rows:
        line = _clean_markdown_line(raw_line)
        if not line or line.startswith("#"):
            continue
        match = SPEAKER_LINE_RE.match(line)
        if match:
            # Template authors often put a prose label such as “录制规则：”
            # directly below the dialogue heading.  It has the same punctuation
            # shape as a spoken line, but must never become a phantom speaker.
            raw_speaker = re.sub(r"\s+", " ", match.group("speaker")).strip("* _·")
            if raw_speaker in NON_DIALOGUE_LABELS:
                continue
            if current:
                raw_turns.append(current)
            speaker = raw_speaker
            current = {
                "raw_turn_id": match.group("turn"),
                "speaker_name": speaker,
                "text": match.group("text").strip(),
                "line_number": line_number,
            }
        elif current and not line.startswith(("!", "[")):
            current["text"] = f"{current['text']} {line}".strip()
    if current:
        raw_turns.append(current)
    if not raw_turns:
        raise ScriptTemplateError("未识别到可导入的“说话人：台词”内容，请选择包含完整对话的模板")

    used_turns: set[str] = set()
    speaker_orders: dict[str, int] = {}
    speaker_ids: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_turns, 1):
        turn_id = _normalise_turn_id(raw.get("raw_turn_id"), index)
        if turn_id in used_turns:
            raise ScriptTemplateError(f"模板中轮次编号重复：{turn_id}")
        used_turns.add(turn_id)
        name = str(raw["speaker_name"])
        if name not in speaker_orders:
            speaker_orders[name] = len(speaker_orders) + 1
            speaker_ids[name] = _speaker_id(name, speaker_orders[name])
        text = str(raw["text"]).strip()
        if not text:
            raise ScriptTemplateError(f"{turn_id} 的台词为空")
        estimate = _estimate_seconds(text)
        if estimate > 20:
            warnings.append(f"{turn_id} 预计约 {estimate:.1f} 秒，超过单段驱动音频 20 秒建议；请在导入后拆分。")
        turns.append({
            "turn_id": turn_id,
            "speaker_id": speaker_ids[name],
            "speaker_name": name,
            "text": text,
            "estimated_seconds": estimate,
            "source_line": int(raw["line_number"]),
        })

    headings = [(len(item.group("level")), item.group("title").strip()) for item in (HEADING_RE.match(line.strip()) for line in markdown.splitlines()) if item]
    h1 = next((title for level, title in headings if level == 1), path.stem)
    h2 = next((title for level, title in headings if level == 2), h1)
    speakers = [
        {"speaker_id": speaker_ids[name], "name": name}
        for name in speaker_orders
    ]
    return {
        "title": h2 or h1,
        "series_title": h1,
        "turns": turns,
        "speakers": speakers,
        "warnings": warnings,
        "source_sha256": _sha256_text(markdown),
        "source_text": markdown,
    }


def _template_path(template_id: str) -> Path:
    raw = str(template_id or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        raise ScriptTemplateError("模板编号不合法")
    if raw.startswith("public/"):
        root = PUBLIC_TEMPLATE_ROOT
        relative = raw.removeprefix("public/")
        if not relative:
            raise ScriptTemplateError("模板编号不合法")
    else:
        # Preserve the historical episode-relative identifier so existing
        # local projects and saved UI selections remain valid.
        root = TEMPLATE_ROOT
        relative = raw
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ScriptTemplateError("模板路径越出内容目录") from exc
    if path.suffix.lower() != ".md" or not path.is_file():
        raise ScriptTemplateError("未找到可导入的 Markdown 模板")
    return path


def list_avatar_script_templates() -> dict[str, Any]:
    templates: list[dict[str, Any]] = []
    sources = (
        ("public", PUBLIC_TEMPLATE_ROOT, "**/*.md"),
        ("episode", TEMPLATE_ROOT, "*/docs/**/*.md"),
    )
    for kind, root, pattern in sources:
        if not root.is_dir():
            continue
        for path in sorted(root.glob(pattern), key=lambda item: item.as_posix().lower()):
            try:
                parsed = _parse_template(path)
            except ScriptTemplateError:
                continue
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            template_id = f"public/{relative}" if kind == "public" else relative
            templates.append({
                "template_id": template_id,
                "episode_id": "public" if kind == "public" else relative.split("/", 1)[0],
                "filename": path.name,
                "title": parsed["title"],
                "series_title": parsed["series_title"],
                "turn_count": len(parsed["turns"]),
                "speakers": parsed["speakers"],
                "source_sha256": parsed["source_sha256"],
            })
    return {"templates": templates}


def preview_avatar_script_template(template_id: str, *, include_source: bool = False) -> dict[str, Any]:
    path = _template_path(template_id)
    parsed = _parse_template(path)
    start = 0.0
    sections: list[dict[str, Any]] = []
    for index, turn in enumerate(parsed["turns"], 1):
        end = round(start + float(turn["estimated_seconds"]), 2)
        sections.append({**turn, "id": f"section-{index:03d}", "start_seconds": start, "end_seconds": end})
        start = end
    result = {
        "template_id": str(template_id).strip().replace("\\", "/"),
        "filename": path.name,
        "title": parsed["title"],
        "series_title": parsed["series_title"],
        "speakers": parsed["speakers"],
        "turns": sections,
        "turn_count": len(sections),
        "estimated_total_duration_seconds": round(start, 2),
        "warnings": parsed["warnings"],
        "source_sha256": parsed["source_sha256"],
    }
    if include_source:
        result["source_text"] = parsed["source_text"]
    return result


def build_avatar_script_from_template(template_id: str, *, speaker_overrides: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the schema-valid script and import provenance, without writing."""
    preview = preview_avatar_script_template(template_id, include_source=True)
    overrides = speaker_overrides or {}
    speaker_ids_by_name: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    for turn in preview["turns"]:
        name = str(turn["speaker_name"])
        speaker_id = str(overrides.get(name) or turn["speaker_id"]).strip().lower()
        if not SPEAKER_ID_RE.fullmatch(speaker_id):
            raise ScriptTemplateError(f"“{name}” 的说话人编号不合法：{speaker_id}")
        assigned = speaker_ids_by_name.get(name)
        if assigned and assigned != speaker_id:
            raise ScriptTemplateError(f"“{name}”在同一模板中被映射为多个说话人编号")
        speaker_ids_by_name[name] = speaker_id
    distinct_names_by_id: dict[str, str] = {}
    for name, speaker_id in speaker_ids_by_name.items():
        other = distinct_names_by_id.get(speaker_id)
        if other and other != name:
            raise ScriptTemplateError(f"说话人编号“{speaker_id}”不能同时绑定“{other}”和“{name}”")
        distinct_names_by_id[speaker_id] = name
    for turn in preview["turns"]:
        name = str(turn["speaker_name"])
        speaker_id = speaker_ids_by_name[name]
        sections.append({
            "id": turn["id"],
            "turn_id": turn["turn_id"],
            "speaker_id": speaker_id,
            "speaker_name": name,
            "expected_asset_filename": f"{turn['turn_id']}_{speaker_id.upper()}.mp4",
            "label": f"{turn['turn_id']} · {name}",
            "text": turn["text"],
            "start_seconds": turn["start_seconds"],
            "end_seconds": turn["end_seconds"],
            "visual_contract": {
                "visual_intent": f"数字人口播：{name} 讲述本段内容",
                "required_assets": [],
                "forbidden_states": ["拉伸或变速驱动音频"],
                "min_visual_coverage": 1,
            },
        })
    script = {
        "version": "1.0",
        "title": preview["title"],
        "total_duration_seconds": preview["estimated_total_duration_seconds"],
        "sections": sections,
        "metadata": {
            "source": "project_template",
            "template_id": preview["template_id"],
            "template_sha256": preview["source_sha256"],
            "timing_basis": "script_estimate_pending_native_avatar_audio",
        },
    }
    provenance = {
        "template_id": preview["template_id"],
        "filename": preview["filename"],
        "title": preview["title"],
        "series_title": preview["series_title"],
        "source_sha256": preview["source_sha256"],
        "warnings": preview["warnings"],
        "source_text": preview["source_text"],
    }
    return script, provenance
