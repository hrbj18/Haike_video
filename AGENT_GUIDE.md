# Haike Video Agent Fast Router

This file is the mandatory first read for every request. It is intentionally short. Do not rebuild context by scanning the repository unless the current task requires it.

## 1. Context routing

- Ordinary explanation, writing, or advice: this file is enough.
- Project development, diagnosis, status, planning, or monitoring: additionally read `docs/handoff/README.md` and `docs/handoff/CURRENT_STATUS.md`.
- Then read at most the relevant topic handoff first:
  - product behavior and invariants: `docs/handoff/PRODUCT_RULES.md`
  - current binding decisions: `docs/handoff/DECISIONS.md`
  - code and test entrypoints: `docs/handoff/CODE_MAP.md`
  - daily technology brief: `docs/handoff/DAILY_TECH_BRIEF.md`
  - GitHub release and cross-machine setup: `docs/handoff/DEPLOYMENT.md`
- Read `PROJECT_CONTEXT.md` only when deeper architecture or repository conventions are needed.
- Old task guides and PRDs are historical evidence. Do not treat them as current rules when they conflict with `docs/handoff/`.
- Do not default-scan `projects/`, `.backlot/daily-runs/`, logs, media, old guides, full Git history, or the whole dirty diff. Inspect only paths needed to answer or change the requested behavior.

After a material implementation, state, decision, blocker, or next-step change, update the smallest applicable handoff file and run:

`python scripts/audit_context_handoff.py`

Do not update handoff files for ordinary questions, acknowledgements, or repeated status checks. Replace stale conclusions instead of appending chat history.

## 2. Request routing

- Answer, explain, review, or report status: inspect read-only evidence and respond. Do not mutate external state.
- Diagnose: identify the cause and evidence. Implement only when the user asks for a fix or the request clearly includes one.
- Change or build: implement, verify in proportion to risk, fix discovered in-scope defects, and report the outcome.
- Monitor or wait: follow the existing producer and task state. Unchanged state is not an error.
- If a choice would materially change scope, cost, external effects, or product behavior and cannot be inferred safely, stop and ask.

## 3. Video-production contract

Actual media production must use the Haike Video pipeline and its manifests. Codex may develop, diagnose, test, or recover that pipeline, but must not become an undocumented replacement producer.

Before a production run:

1. Resolve the project and selected pipeline/manifest.
2. Perform capability and configuration preflight.
   For media-provider choices, use the registry's compact `provider_menu_summary()` first; do not dump the raw support envelope into the task.
3. Read the relevant installed provider/stage skill before invoking that stage.
4. Present or persist provider, model or instance, cost guardrail, aspect ratio, music choice, and review gates.
5. Use idempotent job identifiers and durable state so retries do not duplicate paid work.

Typical stages are: script/source verification, narration or source audio, avatar/video generation, media planning, material acquisition, composition, subtitles, background music, QA, then human review. Audio duration is the master clock; visuals follow audio. Do not stretch speech to fit a visual slot.

Every used asset must have provenance and a stable ID. Avatar media and main visual media are separate layers: an avatar does not mean a scene has a main visual. Locked or frozen slots must not be overwritten by batch operations. Local replacement must not invalidate unrelated segments.

Preview and final render must share the same timeline, subtitle, avatar geometry, crop, and asset contract. Never publish a formal video automatically; automation may stop at a review-ready preview.

## 4. Providers, cost, and failures

- State the exact provider and model/instance before a paid or materially expensive call. Never silently substitute providers or models.
- If several eligible providers exist, follow the project/user selection. If none is explicit, surface the supported choices instead of guessing a costly route.
- RunningHub production defaults to paid-accepted workflow `2094449979141218305` with profile `infinitetalk_448x560_exact_clock_v2` on Standard 24GB (`instanceType=default`). The final PCM16 mono WAV is aligned once to the 25FPS frame clock and its exact frame count is submitted to node 35. Enterprise Lite is not a production target; Plus 48GB remains forbidden unless the user gives a new explicit authorization.
- Enforce project and daily budgets before submission. A paid operation must be recoverable and auditable.
- Failures must preserve completed artifacts and record: stage, provider, error class, retryability, safe resume point, and user-facing Chinese remediation.
- All user-facing UI messages are Chinese; map raw backend errors to actionable Chinese descriptions.

For daily technology brief rules, always use `docs/handoff/DAILY_TECH_BRIEF.md`. Do not revive old fixed `4n+2` or “every news item has four lines” assumptions.

## 5. Quality gates

- Validate schemas and state transitions before running media tools.
- Use at most two independent review passes unless the user explicitly requests more: one structural/semantic pass and one visual/output pass.
- Verify outputs with appropriate evidence: tests, ffprobe/media streams, dimensions, duration, representative frames, subtitles, audio mix, and persisted task status.
- A generated artifact is not complete until it is visible through the intended frontend flow or clearly handed off as a test artifact.
- Preserve completed paid artifacts during retries. Never restart the whole pipeline to fix one local slot.

## 6. Repository work

- Search with `rg` or `rg --files` first.
- Use `apply_patch` for source and documentation edits. Formatting and generated bulk artifacts may use their native tools.
- The worktree may contain user changes. Inspect the relevant diff and preserve unrelated work.
- Do not use destructive Git or filesystem operations unless the user explicitly requests them and the exact target is verified.
- Never expose secrets in logs, UI, tests, or responses. Store credentials only in the repository's ignored local-secret mechanism; redact values from evidence.
- Prefer small, reversible changes and focused tests, then run broader tests when the change crosses contracts.

## 7. Skills and communication

- If the user names a skill, or the task clearly matches an available skill, read its `SKILL.md` completely and follow it.
- Route project-specific pipeline, runtime, music, and taste guidance through `skills/INDEX.md`; it points to the authoritative stage and meta skills without expanding this fast router.
- Announce skill use and any skill-caused action or pause in commentary.
- When tools are needed, send a concise commentary update first and keep the user informed during long work.
- Final responses lead with the outcome, name material files with clickable absolute paths, state what was tested, and call out only genuine remaining risks.

## 8. Authoritative detail sources

- Lightweight current state and routing: `docs/handoff/`
- Architecture and repository conventions: `PROJECT_CONTEXT.md`
- Pipeline manifests and provider capabilities: `config/pipelines/`, `config/providers/`, `config/capability_matrix.yaml`
- Product requirements and historical rationale: `docs/PRD*` and `docs/TASK_GUIDE_*` (historical unless promoted into handoff)
- Project evidence and job state: the selected project directory and `.backlot/` files relevant to that task only

If the handoff package is missing, stale, contradictory, or over its character budget, repair it as part of the current project task before relying on broader historical context.
