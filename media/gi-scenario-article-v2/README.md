# GI Scenario Studio article package

This editorial package tells the Evidence Guide story from the engineering task:
compare GI options, explain why a low-cost option fails, then test whether a model
can select an explanation that follows the evidence.

## Read

- [Article source](article.md) and [styled HTML preview](dist/article.html).
- [Methods and evidence](methods.md) and [methods HTML](dist/methods.html).
- [Previous draft](revisions/article-before-engineering-rewrite.md), retained unchanged.
- [Analysis contract](analysis-contract.md) and [visual contracts](visual-contracts.json).

The main article has two native tables and two responsive explanatory figures.
Full experiment accounting, the output-token diagnostic, and publication recovery
live in the methods notes. Historical ledger and architecture assets remain in
`figures/` and their builders remain in source, but neither is part of the active
article or render manifest.

## Build and inspect

Run from the repository root:

```bash
python3 media/gi-scenario-article-v2/scripts/build_assets.py
media/gi-scenario-film/.venv/bin/python media/gi-scenario-article-v2/scripts/build_preview.py
node media/gi-scenario-article-v2/scripts/render_preview.mjs
python3 media/gi-scenario-article-v2/scripts/verify.py
python3 media/gi-scenario-article-v2/scripts/test_verify.py
```

The preview builder uses the existing isolated film Python environment for its
Markdown parser. The capture script uses Playwright and a locally available
Chromium browser. No application worker dependencies change.

Inspect the desktop and mobile page, figure, and table captures under `dist/`.
`render-audit.json` records font/image loading, local links, table semantics, and
viewport/text overflow. Visual review is still necessary: passing a geometry
check does not establish readable figure composition or a clear narrative.

Generated SVGs are the editable visual sources. The article embeds native-font
PNG renders because browsers restrict custom fonts in external SVG images.
`dist/evidence-manifest.json` records the active evidence projections, source
hashes, and SVG hashes. Verification checks the actual table values and linked
assets, not a minimum article length or an exact closing sentence.

## Scope

- Cedar Trace explains the engineering problem. Its modeled hydraulic results
  do not measure the Guide's usefulness or ecological outcomes.
- The completed agent experiment is synthetic and post-retrieval. Its family
  split is not a large sealed hidden evaluation or a production safety study.
- No practitioner usefulness was measured; no generated study answer was released.
- This rewrite does not change application APIs, agent behavior, experiments, or
  the existing W&B publication. Building it makes no provider or publishing calls.
