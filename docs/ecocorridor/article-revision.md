# EcoCorridor article revision

The reader article is **Why I Built EcoCorridor**, in the existing W&B Report
`VmlldzoxNzc4MjAxMw==`. This is a prose and figure revision, not a new experiment.
The historical technical Report, older green-light article, campaign, traces,
and previous article artifacts remain unchanged.

## Editorial and evidence decisions

The article follows the builder's question, one synthetic portfolio, an
inspectable calculation, the bounded review-brief workflow, and the historical
agent comparison. It uses connected explanations instead of repeated slogans.
The previous title implied a field visit; none occurred in the synthetic study.

The figures have four separate jobs: compare current rasters, show the actual
Evidence Rail, explain draft-release checks, and display the historical input
token comparison. Native text tables carry the portfolio checks and hypothesis
detail. Figure captions, alternate descriptions, sources, and values are pinned
in the asset manifest. The actual Evidence Rail screenshot remains unaltered.

The token totals (10,798 and 10,542) cover the four teaching-portfolio cases,
not all fourteen cases or twenty-two provider conversations. The 2.37% reduction
missed the 25% gate. Fixed traversal and answer hints limit the historical
comparison; the article does not claim adaptive usefulness, practitioner
improvement, or ecological benefit.

## Safe same-ID publication

Run `scripts/publish_ecocorridor_visual_story.py verify` from a clean, pushed Eco
worktree, with `--source-report-url` pointing to the historical technical Report,
`--authorize-source-readback`, and `--output` outside the repository. This checks
the historical source and freezes the target Report's identity, title,
description, serialized blocks, and readable previous text alongside the new
article and figure hashes.

Publish that exact plan with `publish --plan PLAN_PATH
--authorize-source-readback --authorize-synthetic-public-upload
--authorize-report-revision --source-report-url HISTORICAL_REPORT_URL
--output RECEIPT_PATH`. Credentials are loaded in memory; never place them in
arguments, plans, article files, or diagnostics.

The publisher creates a separate article-publication run and immutable artifact
containing the new article/media plus `revision/previous-report.json` and
`revision/previous-text.md`. It records `new_experiment_result=false` and verifies
downloaded bytes. It checks the live target before uploads, after media creation,
and immediately before saving to the same Report ID. Observed edits cause a
failure. The Reports API has no compare-and-swap operation, so a concurrent edit
in the final read/save interval remains a documented limitation.

Retry with the same frozen plan after an uncertain save. Exact revised content
is reused; manual changes after publication are not overwritten. The final
receipt requires the same Report ID, exact revised content, and unchanged
protected Reports.

## Verification

Run the focused publisher tests, the full Python suite, lint, and whitespace
checks. Inspect the four images and article at desktop and narrow widths.
Authenticated API readback verifies content and artifact integrity. A local
responsive preview is layout evidence only, not a claim that a signed-out
browser displayed the private W&B Report.
