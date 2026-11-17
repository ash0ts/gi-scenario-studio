# GI Scenario Studio film

> **Legacy animation status.** The eleven Manim inserts and their render
> pipeline are preserved for historical review, but they are no longer active
> W&B report media and do not run in CI. The seven application walkthrough
> recordings, article source, and report publisher in this directory remain in
> use. The article's active analytical inserts are the five
> [evidence-grounded Canvas films](../gi-scenario-report-films/README.md).

This isolated Manim Community project preserves eleven evidence-grounded
animated inserts for the Cedar Trace application walkthrough. The clips teach
the mental model; the live application remains the source for interaction, run
evidence, comparison, and export.

## Three distinct video artifacts

| Artifact | What it contains | What it does not claim |
|---|---|---|
| Full film | Eleven Manim concept inserts interleaved with twelve separately recorded application segments according to the 27:50 editor script | The conceptual animations are not application footage or hydraulic output |
| Article walkthrough | Seven short recordings of the actual guided React screens, captured automatically from the deterministic browser preview and paired with article text in W&B | The recordings are not Manim excerpts, a packaged-desktop demonstration, or a fresh solver run |
| Optional local rerun | A separate desktop action that can execute all 884 jobs when the approved SWMM binary is packaged | It is not performed or simulated by the browser recording harness |

The article walkthrough is an **interface-only verified replay**. It reads the
hash-verified Cedar Trace fixture and truthfully shows prior exact-run records.
No SWMM process runs during capture, the Run screen does not display fake live
progress, and the Export screen is inspected without creating an export.
Native keychain, filesystem, and desktop-sidecar behavior also remain outside
the browser capture.

## Story boundary

- Every displayed hydraulic number is loaded from the checked-in Cedar Trace
  fixture.
- The story manifest pins the guided fixture and exhaustive benchmark by
  SHA-256; rendering fails until an intentional source-data change is reviewed.
- Conceptual runoff and timing diagrams are visibly labelled as conceptual.
- Search may choose a candidate; it never supplies a reported hydraulic value.
- The film makes no calibration, regulatory, flood-risk, global-optimum,
  construction-feasibility, ML-superiority, energy, or carbon claim.

Before recording, use the artifacts for the role you are performing:

- [Performer script](story/performer-script.md): narration and on-screen actions only.
- [Editor script](story/editor-script.md): timecodes, match cuts, cursor positions,
  and holds.
- [Integrated script](story/integrated-recording-script.md): the complete word-for-word
  narrative and application walkthrough in one document.
- [Companion article](story/article.md): the timing-free, zero-prerequisite written
  story used by the W&B Report publisher.
- [Article media specification](story/article-media.json): the seven section-to-video
  mappings, frame boundaries, captions, and poster frames.
- [Story manifest](story/story-manifest.json): versioned authority for scene order,
  duration, caption cues, handoffs, claims, and filenames.
- [Claim ledger](story/claim-ledger.md): source and limitation for each factual claim.
- [Combined captions](story/combined.srt) and `story/captions/*.srt`: full-film and
  per-segment caption tracks.
- [First-time viewer review](story/viewer-review-checklist.md): the five required
  comprehension outcomes and a timestamped revision record for the assembled cut.

## One-time setup

Manim is isolated from the scientific worker. On macOS:

```bash
brew install cairo pkg-config pango
cd media/gi-scenario-film
uv sync --locked --extra dev
uv run python scripts/verify_fonts.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The film deliberately uses Pango `Text` instead of LaTeX.

## Render

Fast structural smoke render:

```bash
uv run python scripts/render.py --quality smoke
uv run python scripts/validate_outputs.py --profile smoke
```

One draft scene:

```bash
uv run python scripts/render.py --quality draft --scene 01_pareto_trap
```

Production clips and review assets:

```bash
uv run python scripts/render.py --quality production
uv run python scripts/validate_outputs.py --profile production
```

The `Animation` GitHub Actions workflow runs a smoke render on pull requests.
Dispatch it manually with `profile=production` for a clean 1080p render, then
download the `gi-film-<run-id>` artifact within its 14-day retention window.

Generated files are written under `dist/` and intentionally ignored by Git:

- `dist/clips/*.mp4`
- `dist/clips/*.render.json`
- `dist/checkpoints/*-{33,66,end}.png`
- `dist/contact-sheet.jpg`
- `dist/review-contact-sheet.jpg`
- `dist/animation-review-reel.mp4`
- `dist/edit-manifest.json`
- `dist/article-clips/*.mp4`
- `dist/article-clips/posters/*.png`
- `dist/article-clips/manifest.json`

The edit manifest records each clip's target and rendered duration, frame count,
SHA-256 digest, media properties, entry/exit framing, application handoffs, and
checkpoint path. Each clip begins with a 0.5-second clean head handle and ends
with at least a 0.6-second tail handle. Production validation also performs a complete decode and
requires silent H.264/yuv420p, constant-frame-rate 1920x1080 video at 30 fps.
Every clip sidecar binds its bytes to the story, scene code, shared components,
fixture data, benchmark data, fonts, lockfile, render configuration, and tool
versions. Validation rejects stale clips or sources that changed during render.

## Record and package the full-film application footage

Record the twelve application segments from the performer/editor scripts, then
normalize the editorial clips to silent-or-source-audio H.264/yuv420p at
1920x1080, constant 30 fps. Name them exactly:

```text
dist/app-captures/<application-segment-id>.mp4
```

For example, Home is `dist/app-captures/app_home.mp4`. After all twelve clips
exist, validate their target durations and create a hash manifest:

```bash
uv run python scripts/package_recording.py
```

After the narrated edit is assembled, validate and fingerprint the 27:50 master:

```bash
uv run python scripts/package_recording.py --master /absolute/path/to/final-master.mp4
```

This writes `dist/recording-manifest.json`, binding the application captures and
optional master to the story manifest, animation edit manifest, captions,
performer script, and editor script. `uv run pytest` verifies that all 242 cue
texts and timestamps, the combined SRT, and both narration scripts remain exact
projections of the story manifest.

Application footage is recorded separately at constant-frame-rate 1080p30 and
assembled according to `story/editor-script.md`. Browser-preview footage must
be identified as an interface preview; native-only behavior should be recorded
from the packaged desktop application.

## Record the seven article walkthrough clips

These clips come from the guided application, not from the eleven Manim scenes.
The recorder navigates the real React interface at 1440x900, draws a visible
recording cursor, and writes one session video plus exact section markers. The
session manifest binds the capture to both `guidedFixture.json` and
`story/article-media.json` by SHA-256.

From the repository root, start the explicitly labelled browser preview:

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Leave that process running. In a second terminal at the repository root, record
the deterministic walkthrough:

```bash
pnpm record:article-app
```

Use `pnpm record:article-app -- --headed` when a visible browser is useful for
review. The capture produces:

```text
media/gi-scenario-film/dist/article-recording/guided-app-master.webm
media/gi-scenario-film/dist/article-recording/session-markers.json
```

Then normalize the seven marked excerpts to silent, constant-frame-rate H.264
and verify their dimensions, hashes, fixture binding, and complete decodability:

```bash
cd media/gi-scenario-film
uv sync --locked --extra dev --extra publish
uv run python scripts/extract_article_clips.py
uv run python scripts/extract_article_clips.py --check
```

Outputs live under `dist/article-clips/`. Five concise clips are laid out beside
their W&B article copy; the more detailed Compare and Evidence Rail clips are
full width. The layout contract lives in `story/article-media.json`.

## Build and publish the companion W&B Report

The article uses the seven guided-interface recordings above rather than Manim
excerpts. Each recording must stand on its own, clarify one relationship, and
preserve the synthetic-model and interface-replay limitations in its adjacent
caption.

The report also embeds six figures derived from the complete 700-vector exact
oracle and a native interactive W&B comparison of the 120 search replays. Build
and validate that benchmark package from the repository root first:

```bash
uv sync --extra science --extra dev --extra observability
PYTHONPATH=python .venv/bin/python scripts/run_search_benchmark_campaign.py --workers 1
PYTHONPATH=python .venv/bin/python scripts/build_benchmark_publication.py
PYTHONPATH=python .venv/bin/python scripts/publish_benchmark_study.py \
  --entity "$WANDB_REPORT_ENTITY" \
  --project "$WANDB_REPORT_PROJECT"
```

The final command above is a dry run. Add `--publish` only after reviewing the
destination and confirming its exact `RESTRICTED` policy. The article publisher
refuses to proceed without the resulting benchmark receipt, all 120 run
identities, and the pinned dataset and campaign artifacts.

The report's explanation-audit section consumes the completed two-candidate
W&B Serverless Inference campaign under
`dist/evidence-guide-candidate-evaluation/`. Its checked-in release gate is
`DISABLE_LIVE_GUIDE`: neither candidate passed all deterministic and
practitioner gates, so the article must not imply that the live Guide is
enabled. Validate the no-network campaign plan with:

```bash
PYTHONPATH=python uv run --no-sync --no-cache python \
  scripts/run_evidence_guide_candidate_evaluation.py --dry-run
```

The article publisher hash-validates both candidates' outputs, evaluations, and
local receipts against `benchmarks/evidence_guide/v1/release-gate.json`. It
compares numeric/unit fidelity, evidence and limitation coverage, unauthorized
actions, safe abstention, tokens, and latency in a two-row W&B Table. These are
synthetic selection-call results, not runtime reliability or practitioner
evidence.

The canonical campaign publication receipt is
`dist/evidence-guide-candidate-evaluation/weave-publication.json`, produced by
`scripts/evaluate_evidence_guide.py --campaign-dir ... --publish`. The report
accepts that root receipt only after it proves the exact Restricted destination
and matches the release gate, dataset, both candidate receipt hashes,
evaluation-file hashes, failed dispositions, and deterministic summaries. It
then exposes the two SDK-returned evaluation URLs as failure evidence; it never
invents links or treats publication as a passing result.

Agents conversations use a separate receipt contract. The hash-pinned
`benchmarks/evidence_guide/v1/agents-integration-probe.json` records three actual
Granite turns in two synthetic Weave Agents conversations. The production gate
was overridden only in the injected test harness. The probe preserves the
two-turn ALT-017 conversation boundary, starts a clean ALT-207 conversation,
reads every turn back through the Weave SDK, and records trace/root-span identity,
message types, tool activity, root status, and local validation disposition. All
three turns were rejected and released no answer. The report accepts their URLs
only after validating the exact Restricted destination, pinned test contract,
conversation identity, acknowledgement fields, and fail-closed outcome.

If the walkthrough has not yet been recorded, complete the preview, recording,
extraction, and check commands above first. Recheck the clips immediately before
building the publication plan:

```bash
uv sync --locked --extra dev --extra publish
uv run python scripts/extract_article_clips.py --check
```

Preview the exact publication plan without contacting W&B:

```bash
uv run python scripts/publish_wandb_report.py \
  --entity "$WANDB_ENTITY" \
  --project "$WANDB_REPORT_PROJECT"
```

Publishing is an explicit operation. The command logs one deterministic media
run, round-trips and hashes an `evidence-package` artifact, then interleaves
article copy with application-video and exact-benchmark Media Browser panels.
It also logs the two-candidate Evidence Guide comparison Table and pins the
dataset, release gate, provider outputs, deterministic evaluations, and local
candidate receipts in the article artifact, together with the verified campaign
publication receipt and rejected-Agents-probe receipt.
The search section also includes interactive W&B plots built from the maximum-
budget replay runs. Side-by-side application sections use a concise Markdown
panel and a 16:9 video; detailed Compare and Evidence Rail sections remain full
width. The publisher refuses an absent project unless
`--allow-create-project` is supplied, and it refuses to upload unless W&B
reports the destination access as exactly `RESTRICTED`.

```bash
uv run python scripts/publish_wandb_report.py \
  --entity "$WANDB_ENTITY" \
  --project "$WANDB_REPORT_PROJECT" \
  --publish \
  --allow-create-project
```

The publisher never enables a public share link, never reads a dotenv file,
never uploads SWMM engineering evidence, and never writes credentials into the
article, report plan, artifact, or local publication receipt. On subsequent
runs it reuses the report URL recorded under `dist/article-clips/`.
