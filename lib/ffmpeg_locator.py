"""Read-only discovery of the FFmpeg / FFprobe pair used by this project.

VideoCompose owns the single runtime-pair resolver.  This wrapper reuses it so
audio tools never import ``static_ffmpeg`` for side effects, never trigger a
download, and never mix two different ffmpeg builds in one job.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _platform_dir() -> str:
    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _from_environment() -> tuple[str, str] | None:
    binary = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    if not binary:
        return None
    resolved = Path(binary).resolve()
    probe_name = "ffprobe.exe" if resolved.suffix.lower() == ".exe" else "ffprobe"
    probe = shutil.which(probe_name)
    return str(resolved), str(Path(probe).resolve()) if probe else str(resolved.with_name(probe_name))


def _from_video_compose() -> tuple[str, str] | None:
    try:
        from tools.video import video_compose as runtime
    except Exception:
        return None
    for name in ("_discover_ffmpeg_pair", "_ensure_ffmpeg_on_path"):
        resolver = getattr(runtime, name, None)
        if not callable(resolver):
            continue
        try:
            found = resolver()
        except Exception:
            continue
        if isinstance(found, tuple) and len(found) == 2 and all(found):
            return str(found[0]), str(found[1])
        if isinstance(found, str) and found:
            suffix = ".exe" if Path(found).suffix.lower() == ".exe" else ""
            return str(Path(found)), str(Path(found).with_name(f"ffprobe{suffix}"))
    return None


def _from_static_ffmpeg() -> tuple[str, str] | None:
    try:
        spec = importlib.util.find_spec("static_ffmpeg")
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None:
        return None
    suffix = ".exe" if os.name == "nt" else ""
    for location in spec.submodule_search_locations or ():
        root = Path(location)
        for directory in (root / "bin" / _platform_dir(), root / "bin"):
            binary = directory / f"ffmpeg{suffix}"
            probe = directory / f"ffprobe{suffix}"
            if binary.is_file() and probe.is_file():
                return str(binary.resolve()), str(probe.resolve())
    return None


def resolve_ffmpeg_pair() -> tuple[str, str] | None:
    """Return an existing ``(ffmpeg, ffprobe)`` pair, or ``None``."""
    for provider in (_from_environment, _from_video_compose, _from_static_ffmpeg):
        pair = provider()
        if pair and Path(pair[0]).is_file():
            return pair
    return None


def resolve_ffmpeg() -> str | None:
    pair = resolve_ffmpeg_pair()
    return pair[0] if pair else None


def candidate_pairs() -> list[tuple[str, str]]:
    """Every existing pair this project would accept, de-duplicated in order."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for provider in (_from_environment, _from_video_compose, _from_static_ffmpeg):
        pair = provider()
        if not pair or not Path(pair[0]).is_file():
            continue
        key = str(Path(pair[0]).resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(pair)
    return result


_OPTION_SUPPORT: dict[tuple[str, str], bool] = {}


def _normalise_option(option: str) -> str:
    """Accept ``filter_complex_script`` as well as ``-filter_complex_script``.

    The probe is only meaningful for a real flag with its leading dash; probing
    the bare name silently reports "supported" for everything, which is exactly
    the kind of wrong answer this helper exists to prevent.
    """
    token = str(option or "").strip()
    return token if token.startswith("-") else f"-{token}"


def supports_option(binary: str, option: str) -> bool:
    """Whether ``binary`` recognises a command-line option.

    Probing beats assuming.  The binary that happens to be first on ``PATH`` is
    not necessarily the project's own, and a build missing an option only fails
    much later — typically after paid generation has already run.  FFmpeg
    answers ``Missing argument for option`` when it knows an option and
    ``Unrecognized option`` when it does not, which is a cheap, decisive signal.
    """
    token = _normalise_option(option)
    key = (str(binary), token)
    if key in _OPTION_SUPPORT:
        return _OPTION_SUPPORT[key]
    supported = False
    if Path(binary).is_file():
        # FFmpeg reports the offending option *without* its leading dash, e.g.
        # ``Unrecognized option 'filter_complex_script'.`` — matching on the
        # dashed form would silently conclude that every build supports
        # everything, which is the exact failure this probe must not have.
        expected = "Unrecognized option '%s'" % token.lstrip("-")
        try:
            completed = subprocess.run(
                [str(binary), "-hide_banner", "-nostdin", token],
                capture_output=True, text=True, timeout=30,
            )
            output = f"{completed.stdout or ''}{completed.stderr or ''}"
            supported = expected not in output
        except (OSError, subprocess.SubprocessError):
            supported = False
    _OPTION_SUPPORT[key] = supported
    return supported


def resolve_ffmpeg_with_option(option: str) -> tuple[str, str] | None:
    """Return the first known pair whose ffmpeg really accepts ``option``.

    ``-filter_complex_script`` is the motivating case: the avatar assembly hands
    a long filter graph to FFmpeg through a file to stay under the Windows
    command-line limit, and the master builds currently installed on this
    machine do not implement that option at all.
    """
    token = _normalise_option(option)
    for pair in candidate_pairs():
        if supports_option(pair[0], token):
            return pair
    return None
