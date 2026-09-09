"""Download two bounded Pexels supplements and register them in the project."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.workbench import _append_asset, _load_for_write, _now, _probe_duration_seconds, _save
from tools.video.pexels_video import PexelsVideo


PROJECT = ROOT / "projects" / "mihoyo-ai-girlfriend-remake-1"
REQUESTS = (
    ("pexels_waiting_message", "woman waiting smartphone message at night"),
    ("pexels_laptop_companion", "woman working laptop at home night"),
)


def main() -> None:
    state = _load_for_write(PROJECT)
    tool = PexelsVideo()
    results: list[dict] = []
    for tag, query in REQUESTS:
        existing = next(
            (
                asset
                for asset in state.get("assets", [])
                if isinstance(asset.get("generation"), dict)
                and asset["generation"].get("project_role") == tag
                and asset.get("path")
                and (PROJECT / asset["path"]).is_file()
            ),
            None,
        )
        if existing:
            results.append({"role": tag, "asset_id": existing["id"], "path": existing["path"], "reused": True})
            continue
        output = PROJECT / "assets" / "video" / "pexels" / f"{tag}-{uuid4().hex[:8]}.mp4"
        result = tool.execute(
            {
                "query": query,
                "orientation": "portrait",
                "size": "medium",
                "min_duration": 6,
                "max_duration": 30,
                "per_page": 30,
                "page": 1,
                "preferred_quality": "hd",
                "output_path": str(output),
            }
        )
        if not result.success or not output.is_file():
            raise RuntimeError(f"Pexels 未返回 {tag}：{result.error}")
        data = result.data or {}
        duration = _probe_duration_seconds(output, None, float(data.get("duration_seconds") or 0))
        asset = _append_asset(
            PROJECT,
            state,
            {
                "name": f"AI陪伴复刻 · {tag}",
                "type": "video",
                "source_type": "web_download",
                "path": str(output),
                "duration_seconds": duration,
                "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}",
                "provider": "Pexels",
                "source_tool": "pexels_video",
                "license": data.get("license") or "Pexels License (free, no attribution required)",
                "source_url": data.get("pexels_url"),
                "generation": {
                    "project_role": tag,
                    "query": query,
                    "video_id": data.get("video_id"),
                    "result_page": 1,
                    "downloaded_at": _now(),
                },
            },
        )
        results.append({"role": tag, "asset_id": asset["id"], "path": asset["path"], "reused": False})
    _save(PROJECT, state)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
