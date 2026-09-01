# Haike Video

> **Team handoff / new computer / continued development:** start with [START_HERE.md](START_HERE.md). It links the exact deployment steps, published-version status, branch workflow, and agent handoff without requiring a repository-wide search.

Haike Video is a local-first automated video production workbench for turning a title or script into a review-ready preview. It combines editable script drafts, local narration, stock-media planning, digital-human generation, subtitles, background music, and deterministic composition in one recoverable workflow.

The application stops at `review_ready`. It never approves or publishes a formal video automatically.

## Main workflows

- **No-avatar preview:** script → sentence-level narration → visual planning → stock media → subtitles → full preview.
- **Digital-human preview:** script → exact-frame narration → paid-provider confirmation → two-host avatar generation → automatic timing/cutting → visuals → subtitles → full preview.
- **Recoverable tasks:** completed narration and paid avatar results are preserved; safe resume does not resubmit successful work.
- **Composition:** Remotion for structured React scenes and HyperFrames for HTML/GSAP motion-led work.

## Windows requirements

- Windows 10/11 x64
- Git for Windows
- 64-bit Python 3.12
- Node.js 22 or newer
- Internet access for dependency/model downloads and configured cloud providers
- 30 GB free disk space for the base runtime; 50–100 GB is recommended for ongoing media production

## Install

```powershell
git clone https://github.com/hrbj18/Haike_video.git
Set-Location Haike_video
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

The installer creates the Python environments, installs the locked Remotion dependencies, prepares local ASR and TTS runtimes, and creates `.env.local` from `.env.example` when needed.

HyperFrames is resolved through the public npm package on first use. Verify it with:

```powershell
npx hyperframes doctor
```

## Configure

Store credentials only in `.env.local` or `.env.secrets.local`.

- No-avatar production normally needs Pexels and the selected text model.
- Digital-human production additionally needs the RunningHub key/workflow, presenter reference images, and compatible voices.
- Private voice profiles, presenter images, music, projects, models, caches, and paid outputs are intentionally excluded from Git.

## Start

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_backlot.ps1
```

Or double-click `启动工作台.bat`. The default workbench URL is `http://127.0.0.1:4754/`.

## Validate a clean installation

```powershell
.\.venv\Scripts\python.exe scripts\audit_context_handoff.py
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib --basetemp=.p tests\backlot tests\contracts tests\lib tests\tools tests\unit
Set-Location remotion-composer
npm ci --no-audit --no-fund
Set-Location ..
npx hyperframes doctor
```

These checks do not submit paid RunningHub jobs or publish videos. Live provider acceptance must use a short sample, an explicit budget, and a user confirmation before submission.

## Documentation

- [Team handoff — start here](START_HERE.md)
- [Published version status](docs/handoff/RELEASE_STATUS.md)
- [Git branch and version workflow](docs/GIT_WORKFLOW_ZH-CN.md)
- [Changelog](CHANGELOG.md)
- [Windows deployment guide](docs/DEPLOYMENT_WINDOWS_ZH-CN.md)
- [Agent task router](AGENT_GUIDE.md)
- [Current handoff status](docs/handoff/CURRENT_STATUS.md)
- [Provider configuration](docs/PROVIDERS.md)
- [License](LICENSE) and [source attribution](UPSTREAM.md)

## License

Distributed under GNU AGPLv3. See `LICENSE`, `UPSTREAM.md`, and `THIRD_PARTY_NOTICES.md` for the applicable license, modification, and attribution notices.
