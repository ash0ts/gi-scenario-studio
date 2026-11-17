# GI Scenario Studio report films

Five deterministic Canvas films explain the ideas that should not burden the
application interface. They are silent-first, evidence-grounded, and designed
for full-width playback inside the private W&B report.

The checked-in HTML is interactive and seekable. Production rendering creates
1920x1080, 30 fps, silent H.264 MP4 files together with posters, checkpoints,
contact sheets, captions, hashes, and a review reel.

## Evidence boundary

- Cedar Trace is a synthetic teaching fixture.
- Hydraulic values come from checked-in direct SWMM records.
- `Infiltration Loss` is reproduced as a SWMM runoff-accounting term; it is not
  described as groundwater recharge.
- The films do not claim measured field performance, annual benefit, avoided
  flooding, cleaner water, habitat improvement, real cost, energy, or carbon.

## Build and review

From the repository root:

```bash
uv run --no-sync --no-cache python media/gi-scenario-report-films/scripts/build_animation_data.py
node media/gi-scenario-report-films/scripts/render.mjs --stills-only
node media/gi-scenario-report-films/scripts/validate.mjs --stills-only
node media/gi-scenario-report-films/scripts/render.mjs
node media/gi-scenario-report-films/scripts/validate.mjs
```

The renderer requires local Chrome, FFmpeg, and FFprobe. Generated artifacts
live under `dist/` and are ignored by Git.

Publishing is a separate, explicit operation. Its dry run never contacts W&B:

```bash
uv run --no-sync --no-cache python media/gi-scenario-report-films/scripts/publish_wandb.py \
  --entity "$WANDB_ENTITY" \
  --project "$WANDB_REPORT_PROJECT"
```

Add `--publish` only after reviewing the exact Restricted destination.
Publication is deliberately pinned to
`wandb/gi-scenario-studio-article`. The publisher refuses a missing project, a
lookalike destination, or any access policy other than exactly `RESTRICTED`.
It uses the already-published exact-dataset and search-campaign artifacts as
inputs, versions the guided fixture and retained long-event reports as a
separate evidence input, and emits one `analytical-film` artifact. Every file
is downloaded and re-hashed before the local receipt is written.

The publisher never reads `.env` files or accepts a key argument. Provide
`WANDB_API_KEY` in the calling process environment, and keep publication an
explicit operation:

```bash
uv run --no-sync --no-cache python media/gi-scenario-report-films/scripts/publish_wandb.py \
  --entity wandb \
  --project gi-scenario-studio-article \
  --publish
```

The resulting `dist/wandb-publication.json` records the deterministic run,
artifact lineage, animation-manifest hash, Restricted-policy verification,
round-trip verification, and prohibited-run-file audit. The article report
publisher consumes this receipt before updating the existing report URL.
