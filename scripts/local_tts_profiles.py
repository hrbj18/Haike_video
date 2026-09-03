"""Export and import private OpenMontage local TTS profile packs."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / ".backlot" / "tts"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PACK_BYTES = 512 * 1024 * 1024


def _read_profiles(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "profiles.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    return [item for item in payload.get("profiles", []) if isinstance(item, dict) and item.get("id")]


def _write_profiles(data_dir: Path, profiles: list[dict[str, Any]]) -> None:
    path = data_dir / "profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".profiles.json.tmp")
    temporary.write_text(json.dumps({"version": 1, "profiles": profiles}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_relative_audio(data_dir: Path, raw: str) -> tuple[PurePosixPath, Path]:
    relative = PurePosixPath(raw.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"音色包包含不安全的音频路径：{raw}")
    source = (data_dir / Path(*relative.parts)).resolve()
    try:
        source.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"音频路径越过本地数据目录：{raw}") from exc
    return relative, source


def export_pack(data_dir: Path, output: Path, profile_ids: list[str] | None = None) -> dict[str, Any]:
    available = _read_profiles(data_dir)
    selected_ids = set(profile_ids or [str(item["id"]) for item in available])
    selected = [item for item in available if str(item["id"]) in selected_ids]
    missing = selected_ids - {str(item["id"]) for item in selected}
    if missing:
        raise ValueError("找不到要导出的本地音色：" + "、".join(sorted(missing)))
    if not selected:
        raise ValueError("没有可导出的本地音色")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "format": "openmontage-tts-profile-pack", "profiles": selected}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        written: set[str] = set()
        for profile in selected:
            for sample in profile.get("samples", []):
                relative, source = _safe_relative_audio(data_dir, str(sample.get("audio_path") or ""))
                name = relative.as_posix()
                if name in written:
                    continue
                if not source.is_file():
                    raise FileNotFoundError(f"克隆音色参考音频不存在：{source}")
                archive.write(source, name)
                written.add(name)
    return {"output": str(output.resolve()), "profiles": len(selected), "audio_files": len(written)}


def import_pack(data_dir: Path, package: Path) -> dict[str, Any]:
    if not package.is_file() or package.stat().st_size > MAX_PACK_BYTES:
        raise ValueError("音色包不存在或超过 512 MB")
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
            raise ValueError("音色包缺少有效 manifest.json")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"音色包包含不安全路径：{name}")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != "openmontage-tts-profile-pack":
            raise ValueError("不是 OpenMontage 本地音色包")
        incoming = [item for item in manifest.get("profiles", []) if isinstance(item, dict) and item.get("id")]
        if not incoming:
            raise ValueError("音色包没有任何音色")
        audio_files = 0
        for profile in incoming:
            for sample in profile.get("samples", []):
                relative, destination = _safe_relative_audio(data_dir, str(sample.get("audio_path") or ""))
                name = relative.as_posix()
                if name not in names:
                    raise ValueError(f"音色包缺少参考音频：{name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
                audio_files += 1
    merged = {str(item["id"]): item for item in _read_profiles(data_dir)}
    merged.update({str(item["id"]): item for item in incoming})
    _write_profiles(data_dir, list(merged.values()))
    return {"package": str(package.resolve()), "profiles": len(incoming), "audio_files": audio_files}


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage private OpenMontage local TTS profile packs")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--profile-id", action="append", default=[])
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = export_pack(args.data_dir.resolve(), args.output.resolve(), args.profile_id or None)
    else:
        result = import_pack(args.data_dir.resolve(), args.package.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

