"""Software-wide avatar identity library.

The role library intentionally stores only reference material (for example a
transparent three-view turnaround).  A project must still upload one explicit
presenter shot before generation.  This prevents a contact sheet or a random
reference image from silently becoming the image sent to a cloud provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from backlot.state import REPO_ROOT


ROLE_DIRECTORY = REPO_ROOT / ".backlot" / "avatar_roles"
ROLE_FILE = ROLE_DIRECTORY / "roles.json"
ROLE_ASSET_DIRECTORY = ROLE_DIRECTORY / "assets"
ROLE_ID_RE = re.compile(r"^AR-[a-z0-9-]{3,64}$")
ROLE_SLOTS = {"front", "left", "right", "reference"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ROLE_IMAGE_BYTES = 25 * 1024 * 1024


class AvatarRoleError(ValueError):
    """A correctable problem with a global avatar identity asset."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_store() -> dict:
    if not ROLE_FILE.is_file():
        return {"version": 1, "roles": []}
    try:
        value = json.loads(ROLE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "roles": []}
    if not isinstance(value, dict) or not isinstance(value.get("roles"), list):
        return {"version": 1, "roles": []}
    return value


def _save_store(store: dict) -> dict:
    store["updated_at"] = _now()
    _atomic_write(ROLE_FILE, store)
    return store


def _safe_role_id(raw: str) -> str:
    role_id = str(raw or "").strip()
    if not ROLE_ID_RE.fullmatch(role_id):
        raise AvatarRoleError("角色编号不合法")
    return role_id


def _public_role(role: dict) -> dict:
    # Absolute paths are never part of API responses.  The browser gets files
    # through a bounded media endpoint instead.
    value = dict(role)
    value["references"] = [dict(item) for item in role.get("references", []) if isinstance(item, dict)]
    return value


def list_avatar_roles() -> dict:
    store = _read_store()
    return {"version": store.get("version", 1), "roles": [_public_role(role) for role in store["roles"]]}


def get_avatar_role(role_id: str) -> dict:
    role_id = _safe_role_id(role_id)
    for role in _read_store()["roles"]:
        if role.get("role_id") == role_id:
            return _public_role(role)
    raise AvatarRoleError("未找到该数字人角色")


def create_avatar_role(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise AvatarRoleError("请填写角色名称")
    if len(name) > 80:
        raise AvatarRoleError("角色名称不能超过 80 个字符")
    store = _read_store()
    role_id = f"AR-{uuid4().hex[:12]}"
    role = {
        "role_id": role_id,
        "name": name,
        "description": str(payload.get("description") or "").strip()[:500],
        "license": str(payload.get("license") or "仅限本人项目使用").strip()[:200],
        "version": 1,
        "references": [],
        # A role may be the visible identity for one configured TTS profile.
        # It is deliberately an explicit association rather than an inference
        # from a Chinese display name or a file name.
        "voice_binding": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["roles"].append(role)
    _save_store(store)
    return _public_role(role)


def prepare_role_reference_upload(role_id: str, slot: str, original_filename: str) -> tuple[Path, Path]:
    role_id = _safe_role_id(role_id)
    if slot not in ROLE_SLOTS:
        raise AvatarRoleError("角色参考图类型不支持")
    get_avatar_role(role_id)
    filename = Path(original_filename).name
    extension = Path(filename).suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        raise AvatarRoleError("角色参考图仅支持 PNG、JPG、JPEG 或 WEBP")
    directory = ROLE_ASSET_DIRECTORY / role_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{slot}{extension}"
    handle, temporary = tempfile.mkstemp(prefix=".role-upload-", suffix=extension, dir=directory)
    os.close(handle)
    return Path(temporary), target


def _image_metadata(path: Path) -> dict:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if width < 32 or height < 32:
                raise AvatarRoleError("角色参考图至少需要 32×32 像素")
            return {"width": int(width), "height": int(height), "format": str(image.format or "").upper()}
    except (UnidentifiedImageError, OSError) as exc:
        raise AvatarRoleError("上传的文件不是可读取的图片") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_role_reference_upload(role_id: str, slot: str, temporary: Path, target: Path, original_filename: str) -> dict:
    role_id = _safe_role_id(role_id)
    if slot not in ROLE_SLOTS:
        raise AvatarRoleError("角色参考图类型不支持")
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise AvatarRoleError("上传文件为空")
    if temporary.stat().st_size > MAX_ROLE_IMAGE_BYTES:
        raise AvatarRoleError("角色参考图不能超过 25MB")
    metadata = _image_metadata(temporary)
    digest = _sha256(temporary)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)
    store = _read_store()
    for role in store["roles"]:
        if role.get("role_id") != role_id:
            continue
        references = [item for item in role.get("references", []) if item.get("slot") != slot]
        references.append({
            "slot": slot,
            "path": target.relative_to(ROLE_DIRECTORY).as_posix(),
            "original_filename": Path(original_filename).name,
            "sha256": digest,
            "size_bytes": target.stat().st_size,
            "media": metadata,
            "uploaded_at": _now(),
        })
        role["references"] = sorted(references, key=lambda item: item["slot"])
        role["version"] = int(role.get("version") or 0) + 1
        role["updated_at"] = _now()
        _save_store(store)
        return _public_role(role)
    raise AvatarRoleError("角色在上传过程中不存在")


def set_avatar_role_voice_binding(role_id: str, profile: dict | None) -> dict:
    """Attach one runtime voice identity to a reusable avatar role.

    ``profile`` is resolved by the server from the local audio centre.  Store
    only public/non-secret identity fields so a role export never leaks a
    provider token or a provider-private voice resource identifier.
    """
    role_id = _safe_role_id(role_id)
    binding: dict | None = None
    if profile is not None:
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            raise AvatarRoleError("音色编号不能为空")
        binding = {
            "profile_id": profile_id,
            "profile_name": str(profile.get("name") or profile_id).strip()[:120],
            "provider_id": str(profile.get("provider_id") or "").strip()[:80],
            "provider_name": str(profile.get("provider_name") or "").strip()[:120],
            "voice_signature": str(profile.get("voice_signature") or "").strip()[:160] or None,
            "bound_at": _now(),
        }
    store = _read_store()
    if binding:
        for existing in store["roles"]:
            existing_binding = existing.get("voice_binding") if isinstance(existing.get("voice_binding"), dict) else {}
            if existing.get("role_id") != role_id and existing_binding.get("profile_id") == binding["profile_id"]:
                raise AvatarRoleError(f"该音色已关联角色“{existing.get('name') or existing.get('role_id')}”，请先解除原关联")
    for role in store["roles"]:
        if role.get("role_id") != role_id:
            continue
        role["voice_binding"] = binding
        role["version"] = int(role.get("version") or 0) + 1
        role["updated_at"] = _now()
        _save_store(store)
        return _public_role(role)
    raise AvatarRoleError("未找到该数字人角色")


def find_avatar_role_by_voice_profile(profile_id: str) -> dict | None:
    """Return the unique role explicitly bound to a profile, if any."""
    requested = str(profile_id or "").strip()
    if not requested:
        return None
    matches = [
        role for role in _read_store()["roles"]
        if isinstance(role.get("voice_binding"), dict)
        and str(role["voice_binding"].get("profile_id") or "") == requested
    ]
    if len(matches) > 1:
        raise AvatarRoleError("同一音色被关联到多个角色，无法安全选择出镜图")
    return _public_role(matches[0]) if matches else None


def role_front_reference(role: dict) -> dict:
    """Return the only role-library asset eligible as a presenter image."""
    reference = next(
        (item for item in role.get("references", []) if isinstance(item, dict) and item.get("slot") == "front"),
        None,
    )
    if not isinstance(reference, dict):
        raise AvatarRoleError(f"角色“{role.get('name') or role.get('role_id')}”尚未上传正面出镜图")
    return dict(reference)


def avatar_role_asset_file(role_id: str, reference_path: str) -> Path:
    role_id = _safe_role_id(role_id)
    candidate = (ROLE_DIRECTORY / reference_path).resolve()
    allowed = (ROLE_ASSET_DIRECTORY / role_id).resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise AvatarRoleError("角色参考图路径越过了角色目录") from exc
    if not candidate.is_file():
        raise AvatarRoleError("角色参考图文件不存在")
    return candidate
