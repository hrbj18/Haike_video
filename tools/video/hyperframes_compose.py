"""HyperFrames composition tool — HTML/CSS/GSAP render path.

Sibling to `video_compose` (FFmpeg + Remotion). This tool owns the HyperFrames
runtime end-to-end: workspace materialization, `hyperframes lint`,
`hyperframes validate`, and `hyperframes render`. It is invoked by
`video_compose` when `edit_decisions.render_runtime == "hyperframes"`, and
can also be called directly by pipelines that want HyperFrames-specific
operations (lint-only, validate-only, scaffold-only).

This tool deliberately does NOT attempt parity with every Remotion scene
component. See `skills/core/hyperframes.md` for what is in scope in Phase 1
and what remains Remotion-only.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ResumeSupport,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


log = logging.getLogger("hyperframes_compose")


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_VENDORED_GSAP = Path(__file__).resolve().parent / "vendor" / "gsap" / "gsap.min.js"
_WORKSPACE_GSAP_PATH = "assets/runtime/gsap.min.js"


class HyperFramesCompose(BaseTool):
    name = "hyperframes_compose"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "hyperframes"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:npx", "cmd:ffmpeg"]
    install_instructions = (
        "Requires Node.js >= 22 (https://nodejs.org/) and FFmpeg "
        "(https://ffmpeg.org/download.html). The HyperFrames CLI is fetched "
        "on first use via `npx hyperframes` (npm package: `hyperframes`). "
        "Note: the upstream monorepo develops the package as `@hyperframes/cli`, "
        "but it publishes to npm as `hyperframes`. `npx @hyperframes/cli` "
        "returns 404 -- do NOT use that form. Verify setup with "
        "`npx hyperframes doctor` or run the `doctor` operation on this tool."
    )
    agent_skills = [
        "hyperframes",
        "hyperframes-cli",
        "hyperframes-registry",
        "website-to-hyperframes",
        "gsap-core",
        "gsap-timeline",
    ]

    capabilities = [
        "hyperframes_render",
        "hyperframes_lint",
        "hyperframes_validate",
        "hyperframes_inspect",
        "hyperframes_doctor",
        "scaffold_workspace",
        "add_block",
    ]

    best_for = [
        "HTML/CSS/GSAP composition: kinetic typography, product promos, launch reels",
        "Motion-graphics-heavy briefs where the scene library in remotion-composer/ doesn't fit",
        "Website-to-video / UI-driven compositions",
        "Registry-block-driven scenes (hyperframes add data-chart, grain-overlay, etc.)",
    ]
    not_good_for = [
        "Word-level caption burn (stays on Remotion in Phase 1)",
        "Avatar / lip-sync presenter (stays on Remotion in Phase 1)",
        "Existing React scene stack (text_card, stat_card, chart, comparison): reuse Remotion",
    ]
    fallback_tools = ["video_compose"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "render",
                    "lint",
                    "validate",
                    "inspect",
                    "doctor",
                    "scaffold_workspace",
                    "add_block",
                ],
                "description": (
                    "render: materialize workspace + lint + validate + render to MP4. "
                    "lint: run `hyperframes lint` on an existing workspace. "
                    "validate: run `hyperframes validate` (browser-based). "
                    "inspect: run `hyperframes inspect` for layout/motion overflow. "
                    "doctor: run `hyperframes doctor` to check environment. "
                    "scaffold_workspace: materialize HTML/CSS/assets but do not render. "
                    "add_block: run `hyperframes add <name>` to install a registry "
                    "block or component into an existing workspace."
                ),
            },
            "block_name": {
                "type": "string",
                "description": (
                    "Registry block or component name for operation='add_block' "
                    "(e.g. 'data-chart', 'grain-overlay', 'shimmer-sweep'). "
                    "See https://hyperframes.heygen.com/catalog for the list."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Target HyperFrames workspace directory. Typically "
                    "`projects/<name>/hyperframes/`. Required for every op "
                    "except doctor."
                ),
            },
            "output_path": {
                "type": "string",
                "description": "Output MP4 path. Used by operation='render'.",
            },
            "edit_decisions": {
                "type": "object",
                "description": (
                    "Full edit_decisions artifact — required for render and "
                    "scaffold_workspace. Used to generate index.html + CSS."
                ),
            },
            "asset_manifest": {
                "type": "object",
                "description": (
                    "Full asset_manifest artifact — required for render and "
                    "scaffold_workspace. Used to resolve asset IDs to file paths."
                ),
            },
            "playbook": {
                "type": "object",
                "description": (
                    "Loaded playbook dict. Used to drive the style bridge "
                    "(CSS custom properties, typography, motion defaults)."
                ),
            },
            "profile": {
                "type": "string",
                "description": "Media profile name (youtube_landscape, tiktok, instagram_reels, etc.).",
            },
            "quality": {
                "type": "string",
                "enum": ["draft", "standard", "high"],
                "default": "standard",
                "description": "Render quality. `draft` for iterating, `high` for delivery.",
            },
            "fps": {
                "type": "integer",
                "enum": [24, 30, 60],
                "default": 30,
            },
            "strict": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, fail the render on any lint error. Matches "
                    "`hyperframes render --strict`."
                ),
            },
            "skip_contrast": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Skip the WCAG contrast audit during validate. Acceptable "
                    "while iterating; forbidden for final delivery."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=3072, vram_mb=0, disk_mb=2000, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["operation", "workspace_path", "edit_decisions"]
    side_effects = [
        "writes HTML/CSS/JS files into workspace_path",
        "copies asset files into workspace_path/assets/",
        "writes MP4 to output_path",
    ]
    user_visible_verification = [
        "Play the rendered MP4 and verify scene pacing, typography, and audio",
        "Inspect workspace_path/index.html in a browser via `npx hyperframes preview`",
    ]

    # ------------------------------------------------------------------
    # Status / availability
    # ------------------------------------------------------------------

    _NODE_FLOOR_MAJOR = 22
    _NPM_PACKAGE = "hyperframes"  # published npm name (NOT @hyperframes/cli — that's 404)
    # npm metadata is diagnostic only.  Runtime availability is intentionally
    # based on a local executable plus a bounded launch probe: a desktop
    # preflight must not turn red merely because npm is slow or offline.
    _npm_resolve_cache: Optional[dict[str, str]] = None
    _cli_probe_cache: Optional[dict[str, str]] = None
    _local_cli_cache: Optional[dict[str, str]] = None
    _render_blocker_cache: Optional[str] = None

    @classmethod
    def _ensure_desktop_node_toolchain(cls) -> None:
        """Expose bundled Node tooling and a usable Windows browser to HyperFrames.

        HyperFrames' managed chrome-headless-shell can occasionally be
        incompatible with a fully patched Windows installation.  A locally
        installed Chrome is a reliable screenshot-capture fallback.  Honour
        an explicit environment setting first, so production deployments can
        still pin their own browser binary.
        """
        repo_root = Path(__file__).resolve().parents[2]
        local_bin = repo_root / ".local-bin"
        bundled_node = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin"
        candidates = [path for path in (local_bin, bundled_node) if path.is_dir()]
        current = os.environ.get("PATH", "")
        existing = {os.path.normcase(part) for part in current.split(os.pathsep) if part}
        additions = [str(path) for path in candidates if os.path.normcase(str(path)) not in existing]
        if additions:
            os.environ["PATH"] = os.pathsep.join(additions + [current])
        if not os.environ.get("HYPERFRAMES_BROWSER_PATH") and os.name == "nt":
            chrome_candidates = (
                Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files"))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)"))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
            )
            chrome = next((path for path in chrome_candidates if path.is_file()), None)
            if chrome:
                os.environ["HYPERFRAMES_BROWSER_PATH"] = str(chrome)
                os.environ.setdefault("PRODUCER_HEADLESS_SHELL_PATH", str(chrome))

        # Share VideoCompose's read-only FFmpeg discovery.  This covers the
        # user-scoped Winget/static_ffmpeg locations used by the desktop host
        # without downloading or installing anything during preflight.
        try:
            from tools.video.video_compose import _ensure_ffmpeg_on_path

            _ensure_ffmpeg_on_path()
        except (ImportError, OSError):
            pass

    @classmethod
    def _node_major_version(cls) -> Optional[int]:
        """Return Node.js major version, or None if node isn't installed."""
        cls._ensure_desktop_node_toolchain()
        node = shutil.which("node")
        if not node:
            return None
        try:
            out = subprocess.run(
                [node, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
            )
            if out.returncode != 0:
                return None
            match = re.match(r"v?(\d+)\.", out.stdout.strip())
            if not match:
                return None
            return int(match.group(1))
        except (OSError, subprocess.SubprocessError):
            return None

    @classmethod
    def _resolve_npm_package(cls) -> dict[str, str]:
        """Verify the `hyperframes` npm package actually resolves.

        `_runtime_check` previously only verified that node/ffmpeg/npx existed
        on PATH, which meant `runtime_available: True` on any machine with
        Node + FFmpeg — even offline, even if npm was down, even if the
        package was unpublished. This method performs a cheap
        `npm view hyperframes version` (20s timeout) and caches the answer
        for the rest of the process.

        Returns {"version": "X.Y.Z"} on success, {"error": "<short>"} on any
        failure (404, timeout, network error, npm missing). Never raises.
        """
        cls._ensure_desktop_node_toolchain()
        if cls._npm_resolve_cache is not None:
            return cls._npm_resolve_cache

        npm = shutil.which("npm")
        if not npm:
            cls._npm_resolve_cache = {"error": "npm not on PATH"}
            return cls._npm_resolve_cache

        try:
            proc = subprocess.run(
                [npm, "view", cls._NPM_PACKAGE, "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            cls._npm_resolve_cache = {"error": "timeout (20s) — offline or slow registry"}
            return cls._npm_resolve_cache
        except (OSError, subprocess.SubprocessError) as e:
            cls._npm_resolve_cache = {"error": f"npm view failed: {type(e).__name__}"}
            return cls._npm_resolve_cache

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            # Most common failure is 404 (package unpublished or name wrong).
            if "404" in stderr or "E404" in stderr:
                cls._npm_resolve_cache = {
                    "error": f"npm package `{cls._NPM_PACKAGE}` not found (404)"
                }
            else:
                tail = stderr.splitlines()[-1][:200] if stderr else f"exit {proc.returncode}"
                cls._npm_resolve_cache = {"error": f"npm view failed: {tail}"}
            return cls._npm_resolve_cache

        version = (proc.stdout or "").strip()
        if not version:
            cls._npm_resolve_cache = {"error": "npm view returned empty version"}
        else:
            cls._npm_resolve_cache = {"version": version}
        return cls._npm_resolve_cache

    @classmethod
    def _resolve_local_cli(cls) -> dict[str, str]:
        """Resolve an already-installed/cached HyperFrames launcher.

        This method never invokes npm and never downloads a package.  The
        public workbench preflight is read-only, so an online registry lookup
        cannot substitute for an executable that is actually present.
        """
        cls._ensure_desktop_node_toolchain()
        if cls._local_cli_cache is not None:
            return cls._local_cli_cache

        suffix = ".cmd" if os.name == "nt" else ""
        executable_name = f"hyperframes{suffix}"
        repo_root = Path(__file__).resolve().parents[2]
        candidates: list[tuple[Path, str]] = [
            (repo_root / "node_modules" / ".bin" / executable_name, "project_node_modules"),
        ]

        on_path = shutil.which("hyperframes")
        if on_path:
            candidates.append((Path(on_path), "path"))

        cache_roots: list[Path] = []
        configured_cache = os.environ.get("npm_config_cache")
        if configured_cache:
            cache_roots.append(Path(configured_cache))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            cache_roots.append(Path(local_app_data) / "npm-cache")
        app_data = os.environ.get("APPDATA")
        if app_data:
            cache_roots.append(Path(app_data) / "npm-cache")

        seen_roots: set[str] = set()
        for cache_root in cache_roots:
            normalized = os.path.normcase(str(cache_root))
            if normalized in seen_roots:
                continue
            seen_roots.add(normalized)
            npx_root = cache_root / "_npx"
            try:
                cached = sorted(
                    (
                        entry
                        for entry in npx_root.iterdir()
                        if entry.is_dir()
                    ),
                    key=lambda entry: entry.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                cached = []
            candidates.extend(
                (entry / "node_modules" / ".bin" / executable_name, "npm_npx_cache")
                for entry in cached
            )

        seen: set[str] = set()
        for candidate, source in candidates:
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                cls._local_cli_cache = {
                    "path": str(candidate.resolve()),
                    "source": source,
                }
                return cls._local_cli_cache

        cls._local_cli_cache = {
            "error": "local HyperFrames CLI not initialized",
            "reason_code": "hyperframes_cli_missing",
        }
        return cls._local_cli_cache

    @classmethod
    def _probe_cli(cls) -> dict[str, str]:
        """Launch the resolved local CLI with a cheap, bounded probe."""
        cls._ensure_desktop_node_toolchain()
        if cls._cli_probe_cache is not None:
            return cls._cli_probe_cache

        local_cli = cls._resolve_local_cli()
        cli_path = local_cli.get("path")
        if not cli_path:
            cls._cli_probe_cache = {
                "error": local_cli.get("error") or "local HyperFrames CLI not found",
                "reason_code": local_cli.get("reason_code") or "hyperframes_cli_missing",
            }
            return cls._cli_probe_cache

        try:
            proc = subprocess.run(
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
            )
        except subprocess.TimeoutExpired:
            cls._cli_probe_cache = {
                "error": "local CLI launch timed out after 12s",
                "reason_code": "hyperframes_cli_timeout",
            }
            return cls._cli_probe_cache
        except (OSError, subprocess.SubprocessError) as exc:
            cls._cli_probe_cache = {
                "error": f"local CLI launch failed: {type(exc).__name__}",
                "reason_code": "hyperframes_cli_launch_failed",
            }
            return cls._cli_probe_cache

        if proc.returncode != 0:
            output = "\n".join(filter(None, [proc.stderr, proc.stdout])).strip()
            tail = output.splitlines()[-1][:200] if output else f"exit {proc.returncode}"
            cls._cli_probe_cache = {
                "error": f"local CLI launch failed: {tail}",
                "reason_code": "hyperframes_cli_launch_failed",
            }
        else:
            version = (proc.stdout or "").strip().splitlines()[-1][:80]
            cls._cli_probe_cache = {
                "status": "ok",
                "version": version,
                "source": local_cli.get("source") or "local",
                "path": cli_path,
            }
        return cls._cli_probe_cache

    def _runtime_check(self) -> dict[str, Any]:
        """Return availability state for the HyperFrames runtime.

        Checks the controlled local binaries and proves that the already
        installed/cached CLI can start.  npm registry reachability is not a
        runtime prerequisite once the executable is present.
        """
        self._ensure_desktop_node_toolchain()
        node_major = self._node_major_version()
        ffmpeg_path = shutil.which("ffmpeg")
        ffmpeg_ok = ffmpeg_path is not None
        npx_ok = shutil.which("npx") is not None

        reasons: list[str] = []
        reason_code: Optional[str] = None
        if self._render_blocker_cache:
            reasons.append(self._render_blocker_cache)
            reason_code = "hyperframes_previous_render_failed"
        if node_major is None:
            reasons.append("node not found on PATH")
            reason_code = reason_code or "hyperframes_node_missing"
        elif node_major < self._NODE_FLOOR_MAJOR:
            reasons.append(
                f"node major version {node_major} < required {self._NODE_FLOOR_MAJOR}"
            )
            reason_code = reason_code or "hyperframes_node_too_old"
        if not ffmpeg_ok:
            reasons.append("ffmpeg not found on PATH")
            reason_code = reason_code or "hyperframes_ffmpeg_missing"

        cli_probe: dict[str, str] = {}
        if not reasons:
            cli_probe = self._probe_cli()
            if "error" in cli_probe:
                reasons.append(f"local CLI is not executable: {cli_probe['error']}")
                reason_code = cli_probe.get("reason_code") or "hyperframes_cli_launch_failed"

        user_messages = {
            "hyperframes_previous_render_failed": "HyperFrames 上次渲染已失败；请按失败原因修复本地运行时后重新预检。",
            "hyperframes_node_missing": "未找到 Node.js，无法运行 HyperFrames。请先修复项目的本地 Node 运行时。",
            "hyperframes_node_too_old": "Node.js 版本过低，HyperFrames 需要 Node.js 22 或更高版本。",
            "hyperframes_ffmpeg_missing": "未找到项目受控的 FFmpeg/ffprobe，HyperFrames 暂时不能渲染视频。",
            "hyperframes_cli_missing": "本机尚未完成 HyperFrames 本地初始化。请先在联网环境运行一次 HyperFrames 初始化，再重新预检。",
            "hyperframes_cli_timeout": "HyperFrames 本地启动超时。请关闭残留的 Node/渲染进程后重新预检。",
            "hyperframes_cli_launch_failed": "HyperFrames 本地程序无法启动。请检查本地缓存或安装后重新预检。",
        }

        return {
            "runtime_available": not reasons,
            "node_major": node_major,
            "ffmpeg_available": ffmpeg_ok,
            "ffmpeg_path": ffmpeg_path,
            "npx_available": npx_ok,
            "npm_package": self._NPM_PACKAGE,
            "npm_package_version": cli_probe.get("version"),
            "npm_resolve_error": None,
            "cli_probe_status": cli_probe.get("status"),
            "cli_probe_error": cli_probe.get("error"),
            "cli_source": cli_probe.get("source"),
            "reason_code": reason_code,
            "user_message": user_messages.get(reason_code or "", "HyperFrames 当前可用。" if not reasons else "HyperFrames 当前不可用，请修复本地运行时后重新预检。"),
            "reasons": reasons,
        }

    def get_status(self) -> ToolStatus:
        check = self._runtime_check()
        return ToolStatus.AVAILABLE if check["runtime_available"] else ToolStatus.UNAVAILABLE

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        check = self._runtime_check()
        info["hyperframes_runtime"] = check
        if not check["runtime_available"]:
            info["setup_offer"] = {
                "effort": (
                    "1-minute fix"
                    if check["npx_available"] and check["ffmpeg_available"]
                    else "5-minute fix (install Node 22+ and/or FFmpeg)"
                ),
                "install_instructions": self.install_instructions,
                "unlocks": (
                    "HTML/CSS/GSAP composition runtime — kinetic typography, "
                    "product promos, registry blocks, website-to-video."
                ),
            }
        return info

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        ed = inputs.get("edit_decisions") or {}
        cuts = ed.get("cuts", [])
        total = 0.0
        for c in cuts:
            out_s = float(c.get("out_seconds", 0) or 0)
            in_s = float(c.get("in_seconds", 0) or 0)
            total += max(0.0, out_s - in_s)
        return 30.0 + total * 0.5

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        operation = inputs["operation"]
        start = time.time()
        try:
            if operation == "doctor":
                result = self._doctor(inputs)
            elif operation == "scaffold_workspace":
                result = self._scaffold(inputs)
            elif operation == "lint":
                result = self._lint(inputs)
            elif operation == "validate":
                result = self._validate(inputs)
            elif operation == "inspect":
                result = self._inspect(inputs)
            elif operation == "render":
                result = self._render(inputs)
            elif operation == "add_block":
                result = self._add_block(inputs)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            log.exception("hyperframes_compose failed")
            return ToolResult(success=False, error=f"{type(e).__name__}: {e}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _doctor(self, inputs: dict[str, Any]) -> ToolResult:
        """Probe the environment. Reports node/ffmpeg/npx plus CLI doctor output."""
        check = self._runtime_check()
        out: dict[str, Any] = {"runtime_check": check}

        if not check["runtime_available"]:
            return ToolResult(
                success=False,
                error=(
                    "HyperFrames runtime floor not met: "
                    + "; ".join(check["reasons"])
                ),
                data=out,
            )

        # Ask the already-resolved CLI for a deeper check.  `doctor --json`
        # intentionally exits 0 even when checks fail, so `.ok` is the gate.
        try:
            proc = self._run_hf(["doctor", "--json"], cwd=None, timeout=180, check=False)
            payload = self._parse_json_output(proc.stdout)
            out["cli_doctor"] = {
                "exit_code": proc.returncode,
                "report": payload,
                "stdout_tail": (proc.stdout or "")[-4000:],
                "stderr_tail": (proc.stderr or "")[-4000:],
            }
            ok = bool(
                proc.returncode == 0
                and isinstance(payload, dict)
                and payload.get("ok") is True
            )
            return ToolResult(
                success=ok,
                data=out,
                error=None if ok else "hyperframes doctor reported one or more failed checks",
            )
        except Exception as e:
            out["cli_doctor_error"] = str(e)
            return ToolResult(
                success=False,
                error=f"hyperframes doctor failed: {e}",
                data=out,
            )

    def _scaffold(self, inputs: dict[str, Any]) -> ToolResult:
        """Materialize the HyperFrames workspace from Haike Video artifacts.

        This does NOT call `hyperframes init` — we want full control over the
        generated files so they map cleanly to edit_decisions. `init` is
        meant for humans bootstrapping a project by hand.
        """
        workspace = self._require_workspace(inputs)
        edit_decisions = inputs.get("edit_decisions") or {}
        asset_manifest = inputs.get("asset_manifest") or {}
        playbook = inputs.get("playbook") or {}
        profile_name = inputs.get("profile")

        if not edit_decisions.get("cuts"):
            return ToolResult(
                success=False,
                error="edit_decisions with non-empty cuts[] is required for scaffold_workspace",
            )

        width, height, fps = self._resolve_dimensions(profile_name, inputs.get("fps", 30))

        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "compositions").mkdir(exist_ok=True)
        assets_dir = workspace / "assets"
        assets_dir.mkdir(exist_ok=True)

        # HyperFrames validates and renders compositions with network access
        # disabled.  A CDN script can therefore pass static lint but fail at
        # browser validation with ERR_NETWORK_ACCESS_DENIED.  Freeze the GSAP
        # runtime into every workspace so validation, recovery and rendering
        # stay deterministic and offline-safe.
        if not _VENDORED_GSAP.is_file():
            return ToolResult(
                success=False,
                error=f"Vendored GSAP runtime is missing: {_VENDORED_GSAP}",
            )
        runtime_assets_dir = assets_dir / "runtime"
        runtime_assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_VENDORED_GSAP, runtime_assets_dir / "gsap.min.js")

        # Resolve asset IDs → file paths + copy into workspace.
        resolved_cuts, asset_copies = self._resolve_and_stage_assets(
            edit_decisions.get("cuts", []),
            asset_manifest.get("assets", []),
            workspace,
        )

        audio_refs = self._resolve_audio_refs(
            edit_decisions.get("audio", {}),
            asset_manifest.get("assets", []),
            workspace,
        )

        # Style bridge: playbook → CSS custom properties + DESIGN.md.
        css_vars, design_md = self._style_bridge(playbook, edit_decisions)

        # Write hyperframes.json (registry config).
        (workspace / "hyperframes.json").write_text(
            json.dumps(
                {
                    "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
                    "paths": {
                        "blocks": "compositions",
                        "components": "compositions/components",
                        "assets": "assets",
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Write DESIGN.md (convenience file for human review + workspace context).
        if design_md:
            (workspace / "DESIGN.md").write_text(design_md, encoding="utf-8")

        # Write index.html — the main composition.
        total_duration = self._compute_total_duration(resolved_cuts)
        metadata = edit_decisions.get("metadata") if isinstance(edit_decisions.get("metadata"), dict) else {}
        style_context = metadata.get("style_context") if isinstance(metadata.get("style_context"), dict) else None
        if metadata.get("style_pack_id") == "tech-brief-v1" and style_context:
            # Freeze the exact resolved context beside the HTML.  This avoids a
            # later style update changing the historical candidate when it is
            # re-rendered for review or a hot-swap.
            (workspace / "STYLE-PACK.json").write_text(
                json.dumps(style_context, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            html = self._generate_tech_brief_v1_index_html(
                style_context=style_context,
                width=width,
                height=height,
                total_duration=total_duration,
                css_vars=css_vars,
                title=edit_decisions.get("metadata", {}).get("title") or "科技快报",
            )
        else:
            html = self._generate_index_html(
                cuts=resolved_cuts,
                audio_refs=audio_refs,
                width=width,
                height=height,
                total_duration=total_duration,
                css_vars=css_vars,
                title=edit_decisions.get("metadata", {}).get("title")
                or f"Haike Video {edit_decisions.get('renderer_family', 'composition')}",
            )
        (workspace / "index.html").write_text(html, encoding="utf-8")

        return ToolResult(
            success=True,
            data={
                "operation": "scaffold_workspace",
                "workspace": str(workspace),
                "width": width,
                "height": height,
                "fps": fps,
                "total_duration_seconds": total_duration,
                "cut_count": len(resolved_cuts),
                "asset_copies": asset_copies,
                "style_pack_id": metadata.get("style_pack_id"),
            },
            artifacts=[str(workspace / "index.html")],
        )

    def _lint(self, inputs: dict[str, Any]) -> ToolResult:
        workspace = self._require_workspace(inputs)
        if not (workspace / "index.html").exists():
            return ToolResult(
                success=False,
                error=f"No index.html in {workspace}. Run scaffold_workspace first.",
            )
        proc = self._run_hf(["lint", "--json"], cwd=workspace, timeout=120, check=False)
        data: dict[str, Any] = {"exit_code": proc.returncode}
        payload = self._parse_json_output(proc.stdout)
        if payload is not None:
            data["report"] = payload
        else:
            data["stdout_tail"] = (proc.stdout or "")[-4000:]
        data["stderr_tail"] = (proc.stderr or "")[-2000:]
        ok = proc.returncode == 0
        return ToolResult(
            success=ok,
            data=data,
            error=None if ok else f"hyperframes lint exit {proc.returncode}",
        )

    def _validate(self, inputs: dict[str, Any]) -> ToolResult:
        workspace = self._require_workspace(inputs)
        if not (workspace / "index.html").exists():
            return ToolResult(
                success=False,
                error=f"No index.html in {workspace}. Run scaffold_workspace first.",
            )
        args = ["validate", "--json"]
        if inputs.get("skip_contrast"):
            args.append("--no-contrast")
        proc = self._run_hf(args, cwd=workspace, timeout=300, check=False)
        data: dict[str, Any] = {"exit_code": proc.returncode}
        payload = self._parse_json_output(proc.stdout)
        if payload is not None:
            data["report"] = payload
        else:
            data["stdout_tail"] = (proc.stdout or "")[-4000:]
        data["stderr_tail"] = (proc.stderr or "")[-2000:]
        ok = proc.returncode == 0
        return ToolResult(
            success=ok,
            data=data,
            error=None if ok else f"hyperframes validate exit {proc.returncode}",
        )

    def _inspect(self, inputs: dict[str, Any]) -> ToolResult:
        """Inspect a materialized composition for overflow and motion seams.

        ``validate`` checks the declarative HyperFrames contract.  ``inspect``
        samples the rendered layout, which catches the practical failure mode
        most harmful to the workbench: a design that technically renders but
        cuts off a heading or enters the protected caption/presenter area.
        """
        workspace = self._require_workspace(inputs)
        if not (workspace / "index.html").exists():
            return ToolResult(
                success=False,
                error=f"No index.html in {workspace}. Run scaffold_workspace first.",
            )
        args = ["inspect", ".", "--json", "--samples", str(int(inputs.get("samples") or 9)), "--at-transitions"]
        proc = self._run_hf(args, cwd=workspace, timeout=300, check=False)
        data: dict[str, Any] = {"exit_code": proc.returncode}
        payload = self._parse_json_output(proc.stdout)
        if payload is not None:
            data["report"] = payload
        else:
            data["stdout_tail"] = (proc.stdout or "")[-4000:]
        data["stderr_tail"] = (proc.stderr or "")[-2000:]
        ok = proc.returncode == 0
        return ToolResult(
            success=ok,
            data=data,
            error=None if ok else f"hyperframes inspect exit {proc.returncode}",
        )

    @staticmethod
    def _browser_launch_failed(result: ToolResult) -> bool:
        """Distinguish a browser/runtime failure from a composition defect."""
        detail = json.dumps(result.data or {}, ensure_ascii=False)
        markers = (
            "Failed to launch the browser process",
            "Browser process exited with code",
            "STATUS_STACK_BUFFER_OVERRUN",
            "3221225595",
        )
        return any(marker in detail for marker in markers)

    def _add_block(self, inputs: dict[str, Any]) -> ToolResult:
        """Install a registry block or component via `hyperframes add`.

        Blocks are standalone sub-compositions (own dimensions, duration, timeline)
        that land at `compositions/<name>.html`. Components are effect snippets
        that land at `compositions/components/<name>.html`. After install, the
        caller is responsible for wiring the block into `index.html` via
        `data-composition-src` or pasting the component's snippet — see
        `.agents/skills/hyperframes-registry/SKILL.md`.
        """
        workspace = self._require_workspace(inputs)
        block = (inputs.get("block_name") or "").strip()
        if not block:
            return ToolResult(
                success=False,
                error="block_name is required for operation='add_block'",
            )
        if not workspace.exists():
            return ToolResult(
                success=False,
                error=(
                    f"Workspace {workspace} does not exist. Run "
                    "operation='scaffold_workspace' first."
                ),
            )
        args = ["add", block, "--json", "--no-clipboard"]
        proc = self._run_hf(args, cwd=workspace, timeout=300, check=False)
        data: dict[str, Any] = {
            "operation": "add_block",
            "block_name": block,
            "workspace": str(workspace),
            "exit_code": proc.returncode,
        }
        payload = self._parse_json_output(proc.stdout)
        if payload is not None:
            data["report"] = payload
        else:
            data["stdout_tail"] = (proc.stdout or "")[-4000:]
        data["stderr_tail"] = (proc.stderr or "")[-2000:]
        ok = proc.returncode == 0
        return ToolResult(
            success=ok,
            data=data,
            error=None if ok else f"hyperframes add {block} exit {proc.returncode}",
        )

    def _render(self, inputs: dict[str, Any]) -> ToolResult:
        """Full pipeline: scaffold → lint → validate → render."""
        runtime_ok = self._runtime_check()
        if not runtime_ok["runtime_available"]:
            return ToolResult(
                success=False,
                error=(
                    "HyperFrames runtime not available: "
                    + "; ".join(runtime_ok["reasons"])
                    + ". Per governance, this is a blocker — do NOT silently "
                    "fall back to another runtime without user approval."
                ),
                data={"runtime_check": runtime_ok},
            )

        workspace = self._require_workspace(inputs)
        output_path = Path(
            inputs.get("output_path") or (workspace / "renders" / "final.mp4")
        ).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        steps: dict[str, Any] = {}

        # 1. Scaffold — generate HTML/CSS/assets.
        scaffold = self._scaffold(inputs)
        steps["scaffold"] = scaffold.data
        if not scaffold.success:
            return ToolResult(
                success=False,
                error=f"Scaffold failed: {scaffold.error}",
                data={"steps": steps},
            )

        # 2. Lint — static contract checks.
        lint = self._lint({"workspace_path": str(workspace)})
        steps["lint"] = lint.data
        if not lint.success:
            if inputs.get("strict", False):
                return ToolResult(
                    success=False,
                    error=f"Lint failed (strict mode): {lint.error}",
                    data={"steps": steps},
                )
            log.warning("hyperframes lint reported issues (non-strict mode, continuing)")

        # 3. Validate — browser-based contract + contrast.
        validate = self._validate(
            {
                "workspace_path": str(workspace),
                "skip_contrast": inputs.get("skip_contrast", False),
            }
        )
        steps["validate"] = validate.data
        if not validate.success:
            if self._browser_launch_failed(validate):
                blocker = (
                    "HyperFrames browser process cannot start; configure "
                    "HYPERFRAMES_BROWSER_PATH or PRODUCER_HEADLESS_SHELL_PATH "
                    "to a working Chrome executable and retry"
                )
                self.__class__._render_blocker_cache = blocker
                return ToolResult(
                    success=False,
                    error=f"Validate failed: {validate.error}. {blocker}.",
                    data={"steps": steps},
                )
            return ToolResult(
                success=False,
                error=(
                    f"Validate failed: {validate.error}. HyperFrames render "
                    f"is blocked — fix the composition and re-run."
                ),
                data={"steps": steps},
            )

        # 4. Render-time layout inspection is deliberately opt-in so existing
        # generic compositions keep their historical contract. Style packs
        # enable it because their safe regions are a production guarantee.
        metadata = inputs.get("edit_decisions", {}).get("metadata", {}) or {}
        if metadata.get("require_layout_inspect"):
            inspect = self._inspect({"workspace_path": str(workspace), "samples": 9})
            steps["inspect"] = inspect.data
            if not inspect.success:
                if self._browser_launch_failed(inspect):
                    blocker = (
                        "HyperFrames browser process cannot start; configure "
                        "HYPERFRAMES_BROWSER_PATH or PRODUCER_HEADLESS_SHELL_PATH "
                        "to a working Chrome executable and retry"
                    )
                    self.__class__._render_blocker_cache = blocker
                    return ToolResult(
                        success=False,
                        error=f"Inspect failed: {inspect.error}. {blocker}.",
                        data={"steps": steps},
                    )
                return ToolResult(
                    success=False,
                    error=f"Inspect failed: {inspect.error}. HyperFrames render is blocked — fix the layout and re-run.",
                    data={"steps": steps},
                )

        # 5. Render.
        width, height, fps = self._resolve_dimensions(
            inputs.get("profile"), inputs.get("fps", 30)
        )
        quality = inputs.get("quality", "standard")
        args = [
            "render",
            "--output", str(output_path),
            "--fps", str(fps),
            "--quality", quality,
        ]
        proc = self._run_hf(args, cwd=workspace, timeout=1800, check=False)
        steps["render"] = {
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
        if proc.returncode != 0:
            return ToolResult(
                success=False,
                error=f"hyperframes render exit {proc.returncode}",
                data={"steps": steps},
            )

        if not output_path.exists():
            return ToolResult(
                success=False,
                error=(
                    f"hyperframes render exited 0 but output file missing: "
                    f"{output_path}. Check stdout_tail for the real path."
                ),
                data={"steps": steps},
            )

        return ToolResult(
            success=True,
            data={
                "operation": "render",
                "output": str(output_path),
                "workspace": str(workspace),
                "width": width,
                "height": height,
                "fps": fps,
                "quality": quality,
                "steps": steps,
            },
            artifacts=[str(output_path)],
        )

    # ------------------------------------------------------------------
    # Workspace generation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_workspace(inputs: dict[str, Any]) -> Path:
        raw = inputs.get("workspace_path")
        if not raw:
            raise ValueError("workspace_path is required for this operation")
        return Path(raw).resolve()

    @staticmethod
    def _resolve_dimensions(
        profile_name: Optional[str], fps_in: int
    ) -> tuple[int, int, int]:
        """Resolve output dimensions from the media profile, with a safe default."""
        if profile_name:
            try:
                from lib.media_profiles import get_profile  # type: ignore
                # ``tiktok_vertical`` was used by an early workbench draft
                # but is not a registered media-profile name. Preserve old
                # artifacts while preventing a 9:16 job from silently falling
                # back to the generic 1920x1080 default.
                aliases = {"tiktok_vertical": "tiktok", "portrait_9_16": "tiktok"}
                p = get_profile(aliases.get(profile_name, profile_name))
                return int(p.width), int(p.height), int(p.fps)
            except Exception:
                pass
        return 1920, 1080, int(fps_in)

    @staticmethod
    def _compute_total_duration(cuts: list[dict]) -> float:
        if not cuts:
            return 0.0
        return max(float(c.get("out_seconds", 0) or 0) for c in cuts)

    def _resolve_and_stage_assets(
        self,
        cuts: list[dict],
        assets: list[dict],
        workspace: Path,
    ) -> tuple[list[dict], list[dict[str, str]]]:
        """Resolve asset IDs in cuts[].source, copy files into workspace/assets/.

        HyperFrames resolves `src=` relative to the composition HTML file, so
        every asset must live inside the workspace tree. Copying is simpler
        (and portable) than symlinking, at the cost of disk space — these
        are regenerable under `projects/`.
        """
        asset_lookup = {a["id"]: a for a in assets if "id" in a}
        assets_dir = workspace / "assets"
        copies: list[dict[str, str]] = []
        resolved: list[dict] = []
        for cut in cuts:
            source = cut.get("source", "")
            resolved_cut = dict(cut)
            if source in asset_lookup:
                resolved_cut["source"] = asset_lookup[source].get("path", source)
            src_path = Path(resolved_cut["source"]) if resolved_cut.get("source") else None
            if src_path and src_path.exists() and not self._is_inside(src_path, workspace):
                dest = assets_dir / src_path.name
                if not dest.exists() or dest.stat().st_size != src_path.stat().st_size:
                    shutil.copy2(src_path, dest)
                resolved_cut["source"] = str(dest)
                copies.append({"from": str(src_path), "to": str(dest)})
            resolved.append(resolved_cut)
        return resolved, copies

    def _resolve_audio_refs(
        self,
        audio: dict[str, Any],
        assets: list[dict],
        workspace: Path,
    ) -> dict[str, Any]:
        """Resolve narration / music asset IDs and stage them."""
        asset_lookup = {a["id"]: a for a in assets if "id" in a}
        assets_dir = workspace / "assets"
        out: dict[str, Any] = {"narration": [], "music": None}

        for seg in audio.get("narration", {}).get("segments", []) or []:
            aid = seg.get("asset_id")
            if not aid or aid not in asset_lookup:
                continue
            src = Path(asset_lookup[aid].get("path", ""))
            if not src.exists():
                continue
            if not self._is_inside(src, workspace):
                dest = assets_dir / src.name
                if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dest)
            else:
                dest = src
            out["narration"].append(
                {
                    "src": str(dest),
                    "start_seconds": float(seg.get("start_seconds", 0) or 0),
                    "end_seconds": float(seg.get("end_seconds", 0) or 0) or None,
                }
            )

        music = audio.get("music", {})
        m_id = music.get("asset_id")
        if m_id and m_id in asset_lookup:
            src = Path(asset_lookup[m_id].get("path", ""))
            if src.exists():
                if not self._is_inside(src, workspace):
                    dest = assets_dir / src.name
                    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                        shutil.copy2(src, dest)
                else:
                    dest = src
                out["music"] = {
                    "src": str(dest),
                    "volume": float(music.get("volume", 0.15) or 0.15),
                    "fade_in_seconds": float(music.get("fade_in_seconds", 0) or 0),
                    "fade_out_seconds": float(music.get("fade_out_seconds", 0) or 0),
                }

        return out

    @staticmethod
    def _is_inside(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _style_bridge(
        self,
        playbook: dict[str, Any],
        edit_decisions: dict[str, Any],
    ) -> tuple[dict[str, str], str]:
        """Bridge Haike Video playbook → HyperFrames CSS vars + DESIGN.md.

        Delegates to `lib/hyperframes_style_bridge.py` so the logic is
        shareable and testable. Falls back to a safe built-in default when
        the bridge module isn't available.
        """
        try:
            from lib.hyperframes_style_bridge import style_bridge  # type: ignore
            return style_bridge(playbook, edit_decisions)
        except Exception as e:
            log.debug("style_bridge fallback: %s", e)

        vl = (playbook or {}).get("visual_language", {})
        palette = vl.get("color_palette", {})
        typo = (playbook or {}).get("typography", {})

        def _first(raw: Any, default: str) -> str:
            if isinstance(raw, list) and raw:
                return str(raw[0])
            if isinstance(raw, str) and raw:
                return raw
            return default

        bg = _first(palette.get("background"), "#0B0F1A")
        fg = _first(palette.get("text"), "#F5F5F5")
        accent = _first(palette.get("accent"), "#F59E0B")
        primary = _first(palette.get("primary"), "#2563EB")
        heading = typo.get("heading", {}).get("font") or typo.get("heading", {}).get("family") or "Inter"
        body = typo.get("body", {}).get("font") or typo.get("body", {}).get("family") or "Inter"

        css_vars = {
            "--color-bg": bg,
            "--color-fg": fg,
            "--color-accent": accent,
            "--color-primary": primary,
            "--font-heading": heading,
            "--font-body": body,
            "--ease-primary": "cubic-bezier(0.65, 0, 0.35, 1)",
            "--duration-entrance": "0.6s",
        }
        design_md = (
            "# DESIGN\n\n"
            "Generated by Haike Video HyperFrames style bridge (fallback).\n\n"
            f"- Background: `{bg}`\n"
            f"- Foreground: `{fg}`\n"
            f"- Accent: `{accent}`\n"
            f"- Primary: `{primary}`\n"
            f"- Heading font: `{heading}`\n"
            f"- Body font: `{body}`\n"
        )
        return css_vars, design_md

    # ------------------------------------------------------------------
    # HTML generation (minimal, Phase 1)
    # ------------------------------------------------------------------

    def _generate_tech_brief_v1_index_html(
        self,
        *,
        style_context: dict[str, Any],
        width: int,
        height: int,
        total_duration: float,
        css_vars: dict[str, str],
        title: str,
    ) -> str:
        """Emit the frozen Tech Brief V1 HyperFrames composition.

        The composition is intentionally self-contained and deterministic.
        It produces only the support-visual layer.  The workbench overlays
        the presenter video and phrase captions afterwards, which prevents a
        second host or duplicate baked subtitles from reaching the delivery.
        """
        graphic = style_context.get("graphic_copy") if isinstance(style_context.get("graphic_copy"), dict) else {}
        headline_policy = style_context.get("headline_policy") if isinstance(style_context.get("headline_policy"), dict) else {}
        render_headline = headline_policy.get("render_in_hyperframes") is not False
        safe_regions = style_context.get("safe_regions") if isinstance(style_context.get("safe_regions"), dict) else {}
        recipe_key = str(style_context.get("scene_recipe") or "relationship_map")
        # The style package owns the names of these variants.  The renderer
        # keeps a compatibility default so older scene plans stay byte-for-byte
        # recognizable while newly planned scenes can ask for a different
        # composition without introducing a second visual language.
        default_layouts = {
            "headline_statement": "editorial_headline",
            "relationship_map": "radial_map",
            "single_metric": "hero_metric",
            "comparison": "split_columns",
            "process": "vertical_rail",
            "quote_evidence": "claim_evidence",
            "closing_question": "question_hold",
        }
        known_layouts = {
            "headline_statement": {"editorial_headline", "signal_stack"},
            "relationship_map": {"radial_map", "causal_chain", "convergence"},
            "single_metric": {"hero_metric", "metric_ledger"},
            "comparison": {"split_columns", "stacked_duel", "balance_axis"},
            "process": {"vertical_rail", "zigzag_steps"},
            "quote_evidence": {"claim_evidence"},
            "closing_question": {"question_hold"},
        }
        layout_key = str(style_context.get("layout_variant") or default_layouts.get(recipe_key) or "radial_map")
        if layout_key not in known_layouts.get(recipe_key, set()):
            layout_key = default_layouts.get(recipe_key, "radial_map")
        motion_key = str(style_context.get("motion_variant") or "")
        recipe = self._escape_attr(recipe_key)
        layout = self._escape_attr(layout_key)
        motion = self._escape_attr(motion_key)
        aspect = self._escape_attr(str(style_context.get("aspect_profile") or "portrait"))
        headline = self._escape_text(str(graphic.get("headline") or "本段核心信息"))
        headline_html = f'<h1 id="headline" class="headline"><span class="headline-mark">/</span>{headline}</h1>' if render_headline else ''
        headline_tween = 'tl.from("#headline", { y: 44, opacity: 0, duration: .62, ease: "power3.out" }, .34);' if render_headline else ''
        eyebrow = self._escape_text(str(graphic.get("eyebrow") or "科技快报"))
        center_label = self._escape_text(str(graphic.get("center_label") or "核心关系"))
        scene_goal = self._escape_text(str(graphic.get("scene_goal") or graphic.get("headline") or "本段核心信息"))
        supporting = self._escape_text(str(graphic.get("supporting_statement") or graphic.get("scene_goal") or ""))
        nodes = [self._escape_text(str(value)) for value in (graphic.get("nodes") or []) if str(value).strip()][:4]
        if not nodes:
            nodes = ["形成关系", "进入现实", "产生价值"]
        while len(nodes) < 3:
            nodes.append("信息节点")
        vars_css = "\n      ".join(f"{key}: {value};" for key, value in css_vars.items())
        render_tokens = style_context.get("render_tokens") if isinstance(style_context.get("render_tokens"), dict) else {}
        token_colors = render_tokens.get("colors") if isinstance(render_tokens.get("colors"), dict) else {}

        def token_color(name: str, fallback: str) -> str:
            value = str(token_colors.get(name) or "").strip()
            return value if re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else fallback

        paper = token_color("paper", "#F4EEDF")
        paper_deep = token_color("paper_deep", "#E7DDC9")
        ink = token_color("ink", "#12222C")
        ink_soft = token_color("ink_soft", "#35525D")
        # Dark enough to pass WCAG AA against the paper ground.
        orange = token_color("orange", "#C87434")
        green = token_color("green", "#6B9872")
        yellow = token_color("yellow", "#E5C95A")
        pink = token_color("pink", "#D98A99")
        duration = max(0.5, float(total_duration))
        compact = height <= width

        # Two independently laid out, low-risk templates share the same
        # tokens.  Landscape is compatibility-only until its own visual QA;
        # it is not a stretched portrait canvas.
        if compact:
            title_left, title_top, title_width = 600, 116, 1110
            diagram_top, diagram_height = 400, 430
            positions = [(860, 565), (1260, 565), (860, 785), (1260, 785)]
            center_x, center_y = 1060, 690
            presenter = safe_regions.get("presenter") or {"x": .055, "y": .14, "width": .225, "height": .38}
        else:
            title_left, title_top, title_width = 365, 152, 640
            diagram_top, diagram_height = 610, 680
            positions = [(220, 790), (700, 790), (220, 1160), (700, 1160)]
            center_x, center_y = 540, 1000
            presenter = safe_regions.get("presenter") or {"x": .055, "y": .105, "width": .285, "height": .305}
        node_html: list[str] = []
        line_html: list[str] = []
        for index, label in enumerate(nodes):
            x, y = positions[index]
            # The connector uses rotation + length in design pixels.  It is
            # a decoration, not a data claim, so no fake metrics are implied.
            dx, dy = x - center_x, y - center_y
            length = max(1, (dx * dx + dy * dy) ** .5)
            angle = math.degrees(math.atan2(dy, dx))
            node_html.append(
                f'<div id="node-{index}" class="relation-node node-{index}" style="left:{x - 122}px;top:{y - diagram_top - 56}px">{label}</div>'
            )
            line_html.append(
                f'<div class="connector connector-{index}" style="left:{center_x}px;top:{center_y - diagram_top}px;width:{length:.1f}px;transform:rotate({angle:.2f}deg)"><div id="connector-line-{index}" class="connector-line"></div></div>'
            )
        presenter_x = float(presenter.get("x", .055)) * width
        presenter_y = float(presenter.get("y", .105)) * height
        presenter_w = float(presenter.get("width", .285)) * width
        presenter_h = float(presenter.get("height", .305)) * height
        caption = safe_regions.get("caption") if isinstance(safe_regions.get("caption"), dict) else {"y": .81, "height": .135}
        caption_top = float(caption.get("y", .81)) * height
        caption_h = float(caption.get("height", .135)) * height
        motif_repeats = max(0, int(max(0.0, duration - 3.6) / 1.6))
        node_tweens = "\n      ".join(
            f'tl.from("#node-{index}", {{ scale: .72, opacity: 0, duration: .42, ease: "power3.out" }}, {1.35 + index * .13:.2f});'
            for index in range(len(nodes))
        )
        connector_tweens = "\n      ".join(
            f'tl.fromTo("#connector-line-{index}", {{ scaleX: 0, opacity: 0 }}, {{ scaleX: 1, opacity: 1, duration: .32, transformOrigin: "0% 50%", ease: "power2.out" }}, {1.18 + index * .10:.2f});'
            for index in range(len(nodes))
        )
        relation_html = f'''<div class="diagram relation-layout" aria-label="关系图">
        {''.join(line_html)}
        <div id="core" class="core">{center_label}</div>
        {''.join(node_html)}
      </div>'''
        recipe_html = relation_html
        recipe_tweens = f'''tl.from("#core", {{ scale: .58, rotation: -9, opacity: 0, duration: .52, ease: "back.out(1.7)" }}, .98);
      {connector_tweens}
      {node_tweens}'''
        if recipe_key == "relationship_map" and layout_key == "causal_chain":
            chain_cards = "".join(
                f'<div id="chain-{index}" class="chain-card chain-{index}"><span>{index + 1:02d}</span>{label}</div>'
                for index, label in enumerate(nodes[:4])
            )
            recipe_html = f'''<div class="editorial-layout causal-chain-layout" aria-label="因果链图">
        <div class="causal-track"></div>{chain_cards}
        <div id="chain-goal" class="chain-goal">{scene_goal}</div>
      </div>'''
            recipe_tweens = "\n      ".join(
                f'tl.from("#chain-{index}", {{ x: 62, opacity: 0, duration: .42, ease: "power3.out" }}, {0.96 + index * .14:.2f});'
                for index in range(min(len(nodes), 4))
            ) + '\n      tl.from("#chain-goal", { scale: .82, opacity: 0, duration: .42, ease: "back.out(1.45)" }, 1.64);'
        elif recipe_key == "relationship_map" and layout_key == "convergence":
            convergence_cards = "".join(
                f'<div id="converge-{index}" class="converge-card converge-{index}">{label}</div>'
                for index, label in enumerate(nodes[:4])
            )
            convergence_lines = "".join(f'<i class="converge-line converge-line-{index}"></i>' for index in range(min(len(nodes), 4)))
            recipe_html = f'''<div class="editorial-layout convergence-layout" aria-label="汇聚关系图">
        <div class="converge-lines">{convergence_lines}</div>
        {convergence_cards}<div id="converge-core" class="converge-core">{center_label}</div>
        <div id="converge-goal" class="converge-goal">{scene_goal}</div>
      </div>'''
            recipe_tweens = '''tl.from(".converge-card", { y: 38, opacity: 0, duration: .42, stagger: .1, ease: "power3.out" }, .98);
      tl.from(".converge-line", { scaleY: 0, opacity: 0, duration: .32, stagger: .08, transformOrigin: "50% 100%", ease: "power2.out" }, 1.28);
      tl.from("#converge-core", { scale: .62, opacity: 0, duration: .5, ease: "back.out(1.6)" }, 1.5);
      tl.from("#converge-goal", { y: 24, opacity: 0, duration: .38, ease: "power3.out" }, 1.76);'''
        elif recipe_key == "headline_statement" and layout_key == "signal_stack":
            tags = "".join(f'<span id="signal-{index}">{label}</span>' for index, label in enumerate(nodes[:3]))
            recipe_html = f'''<div class="editorial-layout signal-stack-layout">
        <div id="signal-verdict" class="signal-verdict">{scene_goal}</div>
        <div class="signal-tags">{tags}</div>
        <div id="signal-support" class="signal-support">{supporting or center_label}</div>
      </div>'''
            recipe_tweens = '''tl.from("#signal-verdict", { y: 58, opacity: 0, duration: .58, ease: "power3.out" }, .9);
      tl.from(".signal-tags span", { x: 42, opacity: 0, duration: .36, stagger: .1, ease: "power3.out" }, 1.25);
      tl.from("#signal-support", { y: 26, opacity: 0, duration: .38, ease: "power3.out" }, 1.62);'''
        elif recipe_key == "headline_statement":
            recipe_html = f'''<div class="editorial-layout headline-layout">
        <div id="verdict" class="verdict"><span>核心判断</span>{scene_goal}</div>
        <div id="support" class="support-strip">{supporting or center_label}</div>
      </div>'''
            recipe_tweens = '''tl.from("#verdict", { x: 70, opacity: 0, duration: .62, ease: "power3.out" }, .92);
      tl.from("#support", { y: 38, opacity: 0, duration: .48, ease: "power3.out" }, 1.28);'''
        elif recipe_key == "single_metric" and layout_key == "metric_ledger":
            metric_match = re.search(r"\d+(?:\.\d+)?(?:%|倍|亿|万|元|项|台|家|年|月|日)?", str(graphic.get("headline") or "") + str(graphic.get("scene_goal") or ""))
            metric = self._escape_text(metric_match.group(0) if metric_match else center_label)
            ledger_rows = "".join(f'<div class="ledger-row"><span>{index + 1:02d}</span>{label}</div>' for index, label in enumerate(nodes[:3]))
            recipe_html = f'''<div class="editorial-layout ledger-layout">
        <div id="ledger-metric" class="ledger-metric">{metric}</div>
        <div id="ledger-label" class="ledger-label">{scene_goal}</div>
        <div class="ledger-rows">{ledger_rows}</div>
      </div>'''
            recipe_tweens = '''tl.from("#ledger-metric", { x: 56, opacity: 0, duration: .56, ease: "power3.out" }, .9);
      tl.from("#ledger-label", { y: 26, opacity: 0, duration: .4, ease: "power3.out" }, 1.14);
      tl.from(".ledger-row", { x: 38, opacity: 0, duration: .32, stagger: .1, ease: "power3.out" }, 1.34);'''
        elif recipe_key == "single_metric":
            metric_match = re.search(r"\d+(?:\.\d+)?(?:%|倍|亿|万|元|项|台|家|年|月|日)?", str(graphic.get("headline") or "") + str(graphic.get("scene_goal") or ""))
            metric = self._escape_text(metric_match.group(0) if metric_match else center_label)
            recipe_html = f'''<div class="editorial-layout metric-layout">
        <div id="metric" class="metric-value">{metric}</div>
        <div id="metric-label" class="metric-label">{scene_goal}</div>
        <div id="support" class="support-strip">{supporting or (nodes[0] if nodes else center_label)}</div>
      </div>'''
            recipe_tweens = '''tl.from("#metric", { scale: .55, opacity: 0, duration: .58, ease: "back.out(1.6)" }, .9);
      tl.from("#metric-label, #support", { y: 32, opacity: 0, duration: .45, stagger: .14, ease: "power3.out" }, 1.18);'''
        elif recipe_key == "comparison" and layout_key == "stacked_duel":
            recipe_html = f'''<div class="editorial-layout stacked-duel-layout">
        <div id="duel-a" class="duel-card duel-a"><span>路径 A</span>{nodes[0]}</div>
        <div id="duel-b" class="duel-card duel-b"><span>路径 B</span>{nodes[1]}</div>
        <div id="duel-result" class="duel-result">{scene_goal}</div>
      </div>'''
            recipe_tweens = '''tl.from("#duel-a", { x: -56, opacity: 0, duration: .46, ease: "power3.out" }, .92);
      tl.from("#duel-b", { x: 56, opacity: 0, duration: .46, ease: "power3.out" }, 1.06);
      tl.from("#duel-result", { scale: .82, opacity: 0, duration: .4, ease: "back.out(1.4)" }, 1.44);'''
        elif recipe_key == "comparison" and layout_key == "balance_axis":
            recipe_html = f'''<div class="editorial-layout balance-axis-layout">
        <div class="balance-line"></div><div class="balance-pin"></div>
        <div id="balance-a" class="balance-card balance-a">{nodes[0]}</div>
        <div id="balance-b" class="balance-card balance-b">{nodes[1]}</div>
        <div id="balance-result" class="balance-result">{scene_goal}</div>
      </div>'''
            recipe_tweens = '''tl.from("#balance-a", { x: -54, opacity: 0, duration: .46, ease: "power3.out" }, .96);
      tl.from("#balance-b", { x: 54, opacity: 0, duration: .46, ease: "power3.out" }, 1.08);
      tl.from(".balance-line, .balance-pin", { scaleX: 0, opacity: 0, duration: .34, ease: "power2.out" }, 1.3);
      tl.from("#balance-result", { y: 28, opacity: 0, duration: .4, ease: "power3.out" }, 1.54);'''
        elif recipe_key == "comparison":
            recipe_html = f'''<div class="editorial-layout comparison-layout">
        <div id="compare-a" class="compare-card compare-a"><span>路径 A</span>{nodes[0]}</div>
        <div id="compare-b" class="compare-card compare-b"><span>路径 B</span>{nodes[1]}</div>
        <div id="compare-result" class="compare-result">{scene_goal}</div>
      </div>'''
            recipe_tweens = '''tl.from("#compare-a", { x: -64, opacity: 0, duration: .5, ease: "power3.out" }, .92);
      tl.from("#compare-b", { x: 64, opacity: 0, duration: .5, ease: "power3.out" }, 1.04);
      tl.from("#compare-result", { y: 34, opacity: 0, duration: .42, ease: "power3.out" }, 1.42);'''
        elif recipe_key == "process" and layout_key == "zigzag_steps":
            steps = ''.join(f'<div id="zigzag-{index}" class="zigzag-step zigzag-{index}"><span>{index + 1:02d}</span>{label}</div>' for index, label in enumerate(nodes))
            recipe_html = f'''<div class="editorial-layout zigzag-layout">
        <div class="zigzag-track"></div>{steps}
        <div id="zigzag-goal" class="zigzag-goal">{scene_goal}</div>
      </div>'''
            recipe_tweens = "\n      ".join(f'tl.from("#zigzag-{index}", {{ scale: .82, opacity: 0, duration: .38, ease: "back.out(1.35)" }}, {1.0 + index * .14:.2f});' for index in range(len(nodes))) + '\n      tl.from("#zigzag-goal", { y: 24, opacity: 0, duration: .38, ease: "power3.out" }, 1.62);'
        elif recipe_key == "process":
            steps = ''.join(f'<div id="step-{index}" class="process-step"><span>{index + 1:02d}</span>{label}</div>' for index, label in enumerate(nodes))
            recipe_html = f'''<div class="editorial-layout process-layout">
        <div class="process-rail"></div>{steps}
        <div id="process-goal" class="process-goal">{scene_goal}</div>
      </div>'''
            recipe_tweens = "\n      ".join(f'tl.from("#step-{index}", {{ x: 58, opacity: 0, duration: .42, ease: "power3.out" }}, {1.0 + index * .16:.2f});' for index in range(len(nodes))) + '\n      tl.from("#process-goal", { y: 30, opacity: 0, duration: .4, ease: "power3.out" }, 1.62);'
        elif recipe_key == "quote_evidence":
            evidence = ''.join(f'<div id="evidence-{index}" class="evidence-card"><span>证据 {index + 1:02d}</span>{label}</div>' for index, label in enumerate(nodes[:3]))
            recipe_html = f'''<div class="editorial-layout evidence-layout">
        <blockquote id="claim">“{scene_goal}”</blockquote>
        <div class="evidence-grid">{evidence}</div>
      </div>'''
            recipe_tweens = '''tl.from("#claim", { x: -54, opacity: 0, duration: .58, ease: "power3.out" }, .92);
      tl.from(".evidence-card", { y: 36, opacity: 0, duration: .42, stagger: .14, ease: "power3.out" }, 1.3);'''
        elif recipe_key == "closing_question":
            recipe_html = f'''<div class="editorial-layout question-layout">
        <div id="question-mark" class="question-mark" data-layout-allow-occlusion>?</div>
        <div id="question" class="question-copy">{scene_goal}</div>
        <div class="question-options"><span>{nodes[0]}</span><span>{nodes[1]}</span></div>
      </div>'''
            recipe_tweens = '''tl.from("#question-mark", { scale: .45, rotation: -12, opacity: 0, duration: .58, ease: "back.out(1.7)" }, .88);
      tl.from("#question", { y: 42, opacity: 0, duration: .54, ease: "power3.out" }, 1.08);
      tl.from(".question-options span", { y: 28, opacity: 0, duration: .38, stagger: .12, ease: "power3.out" }, 1.45);'''

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{self._escape_text(title)}</title>
  <style>
    :root {{
      {vars_css}
      --paper: {paper};
      --paper-deep: {paper_deep};
      --ink: {ink};
      --ink-soft: {ink_soft};
      --orange: {orange};
      --green: {green};
      --yellow: {yellow};
      --pink: {pink};
      --line: {ink};
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #111; }}
    #tech-brief-v1-root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; color: var(--ink); font-family: var(--font-body); }}
    .clip {{ position: absolute; inset: 0; overflow: hidden; }}
    .scene-bg {{ position: absolute; inset: 0; background: var(--paper); }}
    .scene-bg::before {{ content: ""; position: absolute; inset: 0; opacity: .2; background-image: linear-gradient(90deg, transparent 49.7%, rgba(18,34,44,.34) 50%, transparent 50.3%), linear-gradient(0deg, transparent 49.7%, rgba(18,34,44,.24) 50%, transparent 50.3%); background-size: 72px 72px; }}
    .scene-bg::after {{ content: ""; position: absolute; inset: 0; background: repeating-linear-gradient(0deg, rgba(18,34,44,.035) 0 1px, transparent 1px 5px); mix-blend-mode: multiply; }}
    .frame {{ position: absolute; inset: 38px; border: 4px solid var(--ink); pointer-events: none; }}
    .frame::before {{ content: ""; position: absolute; left: 0; right: 0; top: 142px; border-top: 3px solid var(--ink); }}
    .kicker {{ position: absolute; left: 64px; top: 58px; font-family: var(--font-mono); font-size: 24px; letter-spacing: .14em; font-weight: 700; }}
    .status-dot {{ position: absolute; left: 64px; top: 118px; width: 14px; height: 14px; background: var(--green); border: 2px solid var(--ink); box-shadow: 4px 4px 0 var(--ink); }}
    .status-text {{ position: absolute; left: 90px; top: 114px; font-family: var(--font-mono); font-size: 18px; letter-spacing: .09em; }}
    .headline {{ position: absolute; left: {title_left}px; top: {title_top}px; width: {title_width}px; z-index: 2; font-family: var(--font-heading); font-size: {70 if compact else 84}px; line-height: 1.02; letter-spacing: -.055em; font-weight: 800; }}
    .headline-mark {{ display: inline-block; color: var(--orange); margin-right: 14px; }}
    .presenter-reserve {{ position: absolute; left: {presenter_x:.1f}px; top: {presenter_y:.1f}px; width: {presenter_w:.1f}px; height: {presenter_h:.1f}px; z-index: 1; pointer-events: none; }}
    .diagram {{ position: absolute; left: 0; right: 0; top: {diagram_top}px; height: {diagram_height}px; }}
    .core {{ position: absolute; left: {center_x - 104}px; top: {center_y - diagram_top - 104}px; width: 208px; height: 208px; display:flex; align-items:center; justify-content:center; text-align:center; padding: 24px; background: var(--yellow); border: 4px solid var(--ink); box-shadow: 12px 12px 0 var(--ink); font-size: 32px; line-height: 1.12; font-family: var(--font-heading); font-weight: 800; z-index: 3; }}
    .relation-node {{ position: absolute; width: 244px; min-height: 112px; padding: 22px 20px; display:flex; align-items:center; justify-content:center; text-align:center; background: var(--paper); border: 4px solid var(--ink); box-shadow: 10px 10px 0 var(--ink); font-size: 27px; line-height: 1.18; font-weight: 700; z-index: 2; }}
    .node-0 {{ background: #F6C9CC; }} .node-1 {{ background: #C6DCC7; }} .node-2 {{ background: #F8EBA9; }} .node-3 {{ background: #D6E6ED; }}
    .connector {{ position: absolute; height: 4px; transform-origin: 0 50%; z-index: 1; }}
    /* Connector strokes must begin hidden: HyperFrames rejects delayed full-frame
       overlays that flash before their GSAP entrance tween. */
    .connector-line {{ position: absolute; inset: 0; background: var(--ink); transform-origin: 0 50%; opacity: 0; }}
    .editorial-layout {{ position:absolute; left:7%; right:7%; top:{diagram_top}px; height:{diagram_height}px; z-index:2; }}
    .verdict {{ position:absolute; left:31%; right:0; top:7%; min-height:55%; padding:52px 46px; background:var(--yellow); border:4px solid var(--ink); box-shadow:14px 14px 0 var(--ink); font:800 {54 if compact else 62}px/1.08 var(--font-heading); }}
    .verdict span, .compare-card span, .evidence-card span {{ display:block; margin-bottom:18px; font:700 17px/1 var(--font-mono); letter-spacing:.12em; color:var(--ink-soft); }}
    .support-strip {{ position:absolute; left:5%; right:4%; bottom:5%; padding:28px 34px; border:4px solid var(--ink); background:var(--paper-deep); box-shadow:10px 10px 0 var(--ink); font-size:29px; line-height:1.28; font-weight:700; }}
    .signal-stack-layout {{ padding-left:30%; padding-top:5%; }}
    .signal-verdict {{ min-height:290px; padding:42px 38px; border:4px solid var(--ink); background:var(--yellow); box-shadow:14px 14px 0 var(--ink); font:800 {48 if compact else 58}px/1.08 var(--font-heading); }}
    .signal-tags {{ display:flex; flex-wrap:wrap; gap:14px; margin:28px 0 0 7%; }}
    .signal-tags span {{ padding:17px 22px; border:3px solid var(--ink); background:var(--paper-deep); box-shadow:6px 6px 0 var(--ink); font:800 23px/1.1 var(--font-heading); }}
    .signal-tags span:nth-child(2) {{ background:var(--pink); }} .signal-tags span:nth-child(3) {{ background:var(--green); }}
    .signal-support {{ position:absolute; left:5%; right:4%; bottom:4%; padding:22px 28px; background:var(--ink); color:var(--paper); font:800 27px/1.2 var(--font-heading); }}
    .metric-value {{ position:absolute; left:29%; top:0; color:var(--orange); font:900 {150 if compact else 190}px/.9 var(--font-heading); letter-spacing:-.07em; }}
    .metric-label {{ position:absolute; left:30%; right:0; top:34%; padding:34px; background:var(--ink); color:var(--paper); font:800 40px/1.15 var(--font-heading); }}
    .ledger-layout {{ padding-left:31%; padding-top:1%; }}
    .ledger-metric {{ color:var(--orange); font:900 {132 if compact else 164}px/.9 var(--font-heading); letter-spacing:-.07em; }}
    .ledger-label {{ margin:18px 0 20px; padding:22px 24px; border:4px solid var(--ink); background:var(--yellow); font:800 30px/1.15 var(--font-heading); }}
    .ledger-rows {{ display:grid; gap:12px; }}
    .ledger-row {{ display:flex; gap:18px; align-items:center; min-height:62px; padding:14px 18px; border-left:9px solid var(--green); background:rgba(231,221,201,.88); font:800 23px/1.15 var(--font-heading); }}
    .ledger-row span {{ color:var(--orange); font:800 17px/1 var(--font-mono); }}
    .causal-chain-layout {{ padding:12px 2% 0 32%; }}
    .causal-track {{ position:absolute; left:35%; top:10%; bottom:18%; width:5px; background:var(--ink); }}
    .chain-card {{ position:relative; min-height:78px; margin:0 0 15px 54px; padding:18px 20px; border:4px solid var(--ink); background:var(--paper); box-shadow:7px 7px 0 var(--ink); font:800 24px/1.13 var(--font-heading); }}
    .chain-card span {{ position:absolute; left:-54px; top:16px; width:38px; height:38px; padding-top:10px; text-align:center; border:3px solid var(--ink); background:var(--orange); font:800 14px/1 var(--font-mono); }}
    .chain-card:nth-of-type(3n) {{ background:var(--pink); }} .chain-card:nth-of-type(3n + 1) {{ background:var(--green); }}
    .chain-goal {{ position:absolute; left:7%; right:2%; bottom:1%; padding:19px 24px; border:4px solid var(--ink); background:var(--yellow); font:800 25px/1.14 var(--font-heading); }}
    .convergence-layout {{ padding:20px 3% 0 31%; }}
    .converge-card {{ position:absolute; width:42%; min-height:82px; padding:17px; border:4px solid var(--ink); background:var(--paper-deep); box-shadow:7px 7px 0 var(--ink); font:800 22px/1.13 var(--font-heading); }}
    .converge-0 {{ left:3%; top:7%; }} .converge-1 {{ right:2%; top:7%; background:var(--pink); }} .converge-2 {{ left:3%; top:31%; background:var(--green); }} .converge-3 {{ right:2%; top:31%; background:var(--yellow); }}
    .converge-core {{ position:absolute; left:24%; right:24%; top:54%; min-height:120px; padding:27px 18px; display:flex; align-items:center; justify-content:center; text-align:center; border:4px solid var(--ink); background:var(--orange); box-shadow:10px 10px 0 var(--ink); font:800 31px/1.08 var(--font-heading); z-index:2; }}
    .converge-lines {{ position:absolute; inset:0; pointer-events:none; }}
    .converge-line {{ position:absolute; left:50%; top:35%; width:4px; height:170px; background:var(--ink); transform-origin:50% 100%; }}
    .converge-line-0 {{ transform:rotate(-37deg); }} .converge-line-1 {{ transform:rotate(37deg); }} .converge-line-2 {{ transform:rotate(-20deg); }} .converge-line-3 {{ transform:rotate(20deg); }}
    .converge-goal {{ position:absolute; left:4%; right:2%; bottom:2%; padding:17px 23px; background:var(--ink); color:var(--paper); font:800 24px/1.15 var(--font-heading); }}
    .comparison-layout {{ display:grid; grid-template-columns:1fr 1fr; gap:28px; padding-top:36px; }}
    .compare-card {{ min-height:55%; padding:42px 34px; border:4px solid var(--ink); box-shadow:12px 12px 0 var(--ink); font:800 38px/1.12 var(--font-heading); }}
    .compare-a {{ background:var(--pink); }} .compare-b {{ background:var(--green); }}
    .compare-result {{ position:absolute; left:10%; right:10%; bottom:2%; padding:28px; text-align:center; background:var(--ink); color:var(--paper); font:800 30px/1.2 var(--font-heading); }}
    .stacked-duel-layout {{ padding:1% 1% 0 31%; }}
    .duel-card {{ min-height:194px; margin:0 0 24px; padding:30px 30px; border:4px solid var(--ink); box-shadow:10px 10px 0 var(--ink); font:800 34px/1.12 var(--font-heading); }}
    .duel-card span {{ display:block; margin-bottom:12px; font:800 16px/1 var(--font-mono); letter-spacing:.1em; }}
    .duel-a {{ background:var(--pink); }} .duel-b {{ background:var(--green); }}
    .duel-result {{ position:absolute; left:8%; right:2%; bottom:2%; padding:20px 25px; border:4px solid var(--ink); background:var(--yellow); font:800 26px/1.15 var(--font-heading); }}
    .balance-axis-layout {{ padding:4% 2% 0 31%; }}
    .balance-line {{ position:absolute; left:5%; right:4%; top:44%; height:8px; background:var(--ink); transform-origin:50% 50%; }}
    .balance-pin {{ position:absolute; left:48%; top:38%; width:0; height:0; border-left:34px solid transparent; border-right:34px solid transparent; border-bottom:68px solid var(--orange); }}
    .balance-card {{ position:absolute; top:11%; width:38%; min-height:175px; padding:26px 22px; border:4px solid var(--ink); box-shadow:9px 9px 0 var(--ink); font:800 28px/1.12 var(--font-heading); }}
    .balance-a {{ left:4%; background:var(--green); }} .balance-b {{ right:4%; background:var(--pink); }}
    .balance-result {{ position:absolute; left:7%; right:7%; bottom:8%; padding:23px; text-align:center; background:var(--ink); color:var(--paper); font:800 28px/1.15 var(--font-heading); }}
    .process-layout {{ padding:28px 0 100px 30%; }} .process-rail {{ position:absolute; left:34%; top:12%; bottom:22%; width:5px; background:var(--ink); }}
    .process-step {{ position:relative; margin:0 0 24px 66px; min-height:92px; padding:26px 28px; border:4px solid var(--ink); background:var(--paper); box-shadow:8px 8px 0 var(--ink); font:800 29px/1.14 var(--font-heading); }}
    .process-step span {{ position:absolute; left:-74px; top:13px; width:52px; height:52px; padding-top:13px; text-align:center; border:3px solid var(--ink); background:var(--yellow); font:800 18px/1 var(--font-mono); }}
    .process-goal {{ position:absolute; left:4%; right:0; bottom:1%; padding:22px 28px; background:var(--orange); border:4px solid var(--ink); font:800 27px/1.2 var(--font-heading); }}
    .zigzag-layout {{ padding:1% 3% 0 31%; }}
    .zigzag-track {{ position:absolute; left:52%; top:7%; bottom:21%; width:5px; background:var(--ink); transform:skewY(-14deg); }}
    .zigzag-step {{ position:relative; width:67%; min-height:86px; margin:0 0 18px; padding:19px 20px; border:4px solid var(--ink); background:var(--paper-deep); box-shadow:8px 8px 0 var(--ink); font:800 24px/1.12 var(--font-heading); }}
    .zigzag-step:nth-of-type(odd) {{ margin-left:27%; background:var(--pink); }} .zigzag-step:nth-of-type(even) {{ background:var(--green); }}
    .zigzag-step span {{ display:inline-block; margin-right:12px; color:var(--orange); font:800 16px/1 var(--font-mono); }}
    .zigzag-goal {{ position:absolute; left:5%; right:2%; bottom:1%; padding:18px 22px; border:4px solid var(--ink); background:var(--yellow); font:800 25px/1.14 var(--font-heading); }}
    .evidence-layout blockquote {{ margin:20px 0 34px 30%; padding:36px; background:var(--ink); color:var(--paper); border-left:16px solid var(--orange); font:800 42px/1.13 var(--font-heading); }}
    .evidence-grid {{ margin-left:8%; display:grid; grid-template-columns:repeat(3,1fr); gap:18px; }}
    .evidence-card {{ min-height:210px; padding:30px 24px; border:4px solid var(--ink); background:var(--paper-deep); box-shadow:8px 8px 0 var(--green); font:800 26px/1.2 var(--font-heading); }}
    .question-mark {{ position:absolute; left:8%; top:-6%; color:var(--orange); font:900 260px/.8 var(--font-heading); }}
    .question-copy {{ position:absolute; left:31%; right:2%; top:5%; padding:42px; border:4px solid var(--ink); background:var(--yellow); box-shadow:14px 14px 0 var(--ink); font:800 48px/1.12 var(--font-heading); }}
    .question-options {{ position:absolute; left:16%; right:3%; bottom:7%; display:flex; gap:22px; }}
    .question-options span {{ flex:1; padding:24px; border:4px solid var(--ink); background:var(--paper-deep); font:800 26px/1.16 var(--font-heading); }}
    .caption-reserve {{ position: absolute; left: 7%; width: 86%; top: {caption_top:.1f}px; height: {caption_h:.1f}px; pointer-events: none; }}
    .recipe-tag {{ position: absolute; right: 66px; top: 66px; padding: 10px 14px; border: 3px solid var(--ink); background: var(--orange); box-shadow: 6px 6px 0 var(--ink); font-family: var(--font-mono); font-size: 16px; font-weight: 700; letter-spacing: .06em; }}
    [data-aspect="landscape"] .frame {{ inset: 28px; }}
    [data-aspect="landscape"] .kicker {{ left: 52px; top: 50px; }}
    [data-aspect="landscape"] .status-dot {{ left: 52px; top: 86px; }}
    [data-aspect="landscape"] .status-text {{ left: 78px; top: 83px; }}
    [data-aspect="landscape"] .recipe-tag {{ right: 52px; top: 50px; }}
    [data-aspect="landscape"] .process-layout {{ padding:10px 0 96px 30%; }}
    [data-aspect="landscape"] .process-rail {{ top:6%; bottom:28%; }}
    [data-aspect="landscape"] .process-step {{ min-height:62px; margin:0 0 10px 66px; padding:16px 22px; font-size:23px; }}
    [data-aspect="landscape"] .process-step span {{ top:7px; width:44px; height:44px; padding-top:10px; }}
    [data-aspect="landscape"] .process-goal {{ padding:15px 22px; font-size:22px; z-index:2; }}
  </style>
  <script src="{_WORKSPACE_GSAP_PATH}"></script>
</head>
<body>
  <main id="tech-brief-v1-root" data-composition-id="tech-brief-v1" data-start="0" data-duration="{self._f(duration)}" data-width="{width}" data-height="{height}" data-aspect="{aspect}">
    <section id="tech-brief-scene" class="clip" data-start="0" data-duration="{self._f(duration)}" data-track-index="1" data-recipe="{recipe}" data-layout="{layout}" data-motion="{motion}">
      <div class="scene-bg"></div>
      <div class="frame"></div>
      <div class="kicker">{eyebrow}</div>
      <div class="status-dot"></div><div class="status-text">TECH BRIEF / VERIFIED CONTEXT</div>
      <div class="recipe-tag">{recipe.upper()} / {layout.upper()}</div>
      <div class="presenter-reserve" aria-hidden="true"></div>
      {headline_html}
      {recipe_html}
      <div class="caption-reserve" aria-hidden="true"></div>
    </section>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      tl.from(".frame", {{ opacity: 0, duration: .3, ease: "power1.out" }}, 0);
      tl.from(".kicker, .status-dot, .status-text, .recipe-tag", {{ y: -18, opacity: 0, duration: .42, stagger: .06, ease: "power3.out" }}, .12);
      {headline_tween}
      {recipe_tweens}
      {'tl.to(".relation-node", { y: -8, duration: .55, yoyo: true, repeat: ' + str(max(0, motif_repeats * 2 - 1)) + ', stagger: .06, ease: "sine.inOut" }, 3.25);' if motif_repeats and recipe_key == "relationship_map" and layout_key == "radial_map" else ''}
      window.__timelines["tech-brief-v1"] = tl;
    </script>
  </main>
</body>
</html>
"""

    def _generate_index_html(
        self,
        cuts: list[dict],
        audio_refs: dict[str, Any],
        width: int,
        height: int,
        total_duration: float,
        css_vars: dict[str, str],
        title: str,
    ) -> str:
        """Emit a HyperFrames-contract-compliant index.html.

        Phase 1 covers the minimum required for smoke-testing the runtime:
        - still images (img.clip)
        - video clips (video.clip, muted playsinline + separate audio if needed)
        - text cards (div.clip with styled <h1>)
        - narration segments (audio)
        - music bed (audio, lower volume)

        Richer scene types (registry blocks, kinetic typography) are authored
        by the agent directly into compositions/ — this generator just
        provides a functional starting skeleton.
        """
        vars_css = "\n      ".join(f"{k}: {v};" for k, v in css_vars.items())

        clip_html: list[str] = []
        entrance_tweens: list[str] = []
        for i, cut in enumerate(cuts):
            html, tween = self._cut_to_html(i, cut, width, height)
            clip_html.append(html)
            if tween:
                entrance_tweens.append(tween)

        audio_html: list[str] = []
        for j, nar in enumerate(audio_refs.get("narration") or []):
            src = self._rel_from_workspace(nar["src"])
            start = nar.get("start_seconds", 0)
            end = nar.get("end_seconds")
            duration = (end - start) if end and end > start else (total_duration - start)
            audio_html.append(
                f'<audio id="nar-{j}" '
                f'data-start="{self._f(start)}" data-duration="{self._f(duration)}" '
                f'data-track-index="2" src="{self._escape_attr(src)}" '
                f'data-volume="1"></audio>'
            )

        music = audio_refs.get("music")
        if music:
            src = self._rel_from_workspace(music["src"])
            audio_html.append(
                f'<audio id="music" '
                f'data-start="0" data-duration="{self._f(total_duration)}" '
                f'data-track-index="3" src="{self._escape_attr(src)}" '
                f'data-volume="{self._f(music["volume"])}"></audio>'
            )

        tween_block = "\n        ".join(entrance_tweens) if entrance_tweens else "// no tweens"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{self._escape_text(title)}</title>
  <style>
    :root {{
      {vars_css}
    }}
    body {{ margin: 0; background: var(--color-bg); color: var(--color-fg); font-family: var(--font-body); }}
    [data-composition-id="root"] {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
    }}
    .clip {{ position: absolute; inset: 0; }}
    .clip.video-clip, .clip.image-clip {{ object-fit: cover; width: 100%; height: 100%; }}
    .clip.text-card {{ display: flex; align-items: center; justify-content: center; padding: 120px 160px; box-sizing: border-box; text-align: center; }}
    .clip.text-card h1 {{ font-family: var(--font-heading); font-weight: 700; font-size: 96px; line-height: 1.1; margin: 0; color: var(--color-fg); }}
    .clip.text-card .subtitle {{ font-size: 36px; margin-top: 24px; color: var(--color-accent); }}
  </style>
  <script src="{_WORKSPACE_GSAP_PATH}"></script>
</head>
<body>
  <div data-composition-id="root" data-start="0" data-duration="{self._f(total_duration)}" data-width="{width}" data-height="{height}">
    {"".join(clip_html)}
    {"".join(audio_html)}
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      {tween_block}
      window.__timelines["root"] = tl;
    </script>
  </div>
</body>
</html>
"""

    def _cut_to_html(
        self, index: int, cut: dict, width: int, height: int
    ) -> tuple[str, Optional[str]]:
        """Render one cut + its entrance tween. Returns (html, tween or None)."""
        cut_id = f"cut-{index}"
        in_s = float(cut.get("in_seconds", 0) or 0)
        out_s = float(cut.get("out_seconds", 0) or 0)
        duration = max(0.1, out_s - in_s)

        source = cut.get("source") or ""
        cut_type = (cut.get("type") or "").lower()
        text = cut.get("text") or cut.get("title") or ""

        src_path = Path(source) if source else None
        ext = src_path.suffix.lower() if src_path else ""

        # Decide scene shape
        if cut_type in {"text_card", "hero_title", "callout"} or (not source and text):
            inner = f'<h1>{self._escape_text(text or f"Scene {index + 1}")}</h1>'
            subtitle = cut.get("subtitle") or cut.get("caption")
            if subtitle:
                inner += f'<div class="subtitle">{self._escape_text(subtitle)}</div>'
            html = (
                f'<div id="{cut_id}" class="clip text-card" '
                f'data-start="{self._f(in_s)}" data-duration="{self._f(duration)}" '
                f'data-track-index="1">{inner}</div>'
            )
            # Mild entrance — fade + lift.
            tween = (
                f'tl.from("#{cut_id} h1", {{ y: 40, opacity: 0, duration: 0.6, '
                f'ease: "power3.out" }}, {self._f(in_s + 0.1)});'
            )
            return html, tween

        if ext in _IMAGE_EXTENSIONS and src_path:
            rel = self._rel_from_workspace(str(src_path))
            html = (
                f'<img id="{cut_id}" class="clip image-clip" '
                f'src="{self._escape_attr(rel)}" '
                f'data-start="{self._f(in_s)}" data-duration="{self._f(duration)}" '
                f'data-track-index="1" alt="">'
            )
            tween = (
                f'tl.from("#{cut_id}", {{ scale: 1.05, opacity: 0, duration: 0.5, '
                f'ease: "power2.out" }}, {self._f(in_s)});'
            )
            return html, tween

        if ext in _VIDEO_EXTENSIONS and src_path:
            rel = self._rel_from_workspace(str(src_path))
            html = (
                f'<video id="{cut_id}" class="clip video-clip" '
                f'src="{self._escape_attr(rel)}" '
                f'data-start="{self._f(in_s)}" data-duration="{self._f(duration)}" '
                f'data-track-index="1" muted playsinline></video>'
            )
            return html, None

        # Unknown cut shape — render a placeholder text card so the render
        # still succeeds; lint/validate will surface the issue.
        if ext in {".html", ".htm"} and src_path:
            rel = self._rel_from_workspace(str(src_path))
            composition_id = Path(rel).stem
            html = (
                f'<div id="{cut_id}" class="clip composition-clip" '
                f'data-composition-id="{self._escape_attr(composition_id)}" '
                f'data-composition-src="{self._escape_attr(rel)}" '
                f'data-start="{self._f(in_s)}" data-duration="{self._f(duration)}" '
                f'data-width="{width}" data-height="{height}" '
                f'data-track-index="1"></div>'
            )
            return html, None

        placeholder = self._escape_text(text or cut.get("reason") or f"Scene {index + 1}")
        html = (
            f'<div id="{cut_id}" class="clip text-card" '
            f'data-start="{self._f(in_s)}" data-duration="{self._f(duration)}" '
            f'data-track-index="1"><h1>{placeholder}</h1></div>'
        )
        return html, None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _run_hf(
        self,
        args: list[str],
        *,
        cwd: Optional[Path],
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess:
        """Invoke the already-resolved local HyperFrames CLI.

        We intentionally bypass `self.run_command` here because we do NOT
        want to raise CalledProcessError on non-zero exits — the caller
        parses lint/validate/render exit codes itself.
        """
        self._ensure_desktop_node_toolchain()
        local_cli = self._resolve_local_cli()
        cli_path = local_cli.get("path")
        if not cli_path:
            raise RuntimeError(local_cli.get("error") or "local HyperFrames CLI not found")
        cmd = [cli_path, *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            # Surface timeouts as a failed CompletedProcess so callers get a
            # uniform shape. The stderr tail will say timeout.
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=124,
                stdout=e.stdout or "",
                stderr=(e.stderr or "") + f"\n[timeout after {timeout}s]",
            )

    @staticmethod
    def _parse_json_output(stdout: str) -> Optional[Any]:
        """Parse a `--json` report, tolerating surrounding banner lines."""
        if not stdout:
            return None
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(stdout[start : end + 1])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _f(v: float) -> str:
        return f"{float(v):.3f}".rstrip("0").rstrip(".")

    @staticmethod
    def _escape_text(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _escape_attr(s: str) -> str:
        return HyperFramesCompose._escape_text(s).replace('"', "&quot;")

    @staticmethod
    def _rel_from_workspace(path: str) -> str:
        """HyperFrames resolves src= relative to index.html. Our asset files
        live under workspace/assets/, so when we stage a copy we know the
        relative path is `assets/<name>`. For files already in the workspace
        tree, fall back to the file name.
        """
        p = Path(path)
        # If it's already a relative path starting with assets/, keep as-is.
        if not p.is_absolute():
            return str(p).replace("\\", "/")
        parts = p.parts
        for anchor in ("assets", "compositions"):
            if anchor in parts:
                index = len(parts) - 1 - list(reversed(parts)).index(anchor)
                return "/".join(parts[index:])
        # Otherwise emit just the basename under assets/.
        return f"assets/{p.name}"
