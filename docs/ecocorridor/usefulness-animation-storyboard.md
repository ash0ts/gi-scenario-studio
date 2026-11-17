# Usefulness evidence animation — gated storyboard

This is a rendering contract, not a result. No new MP4, poster, transcript, or
W&B media panel may be produced from this storyboard until a completed campaign
artifact, exact Weave traces, and the claim ledger authorize every displayed
statement.

## Audience and single takeaway

The audience is a technical reviewer deciding whether EcoCorridor or its
read-only agent reduces screening-review burden. The one takeaway is:

> A bounded workflow claim is earned only when fair evidence conditions lead
> to a measured review outcome that reconciles to direct Circuitscape evidence.

Nothing in the film may imply observed animal movement, crossing use, collision
reduction, population response, biodiversity benefit, regulatory approval, or
a design-ready crossing.

## Runtime and frame contract

- Silent H.264 MP4, 16:9, 1920×1080, 30 fps, 14–18 seconds.
- One idea per scene; no text smaller than 32 px at 1920×1080.
- Basalt is the background/evidence color, current cyan identifies direct
  evidence, lichen identifies accepted deterministic checks, caution amber
  identifies unresolved review, and safeguard berry identifies a blocked or
  unsafe release.
- Normal motion uses eased translation and opacity only. Reduced-motion output
  uses the same semantic frames with crossfades and no map scrubbing.
- The poster is the bounded-conclusion frame. The transcript describes every
  value, comparison, and state transition.

## Data-bound scenes

| Time | Scene | Required artifact fields | Visual statement |
|---|---|---|---|
| 0–2 s | Decision task | case ID, landscape family, frozen rubric hash | A reviewer must reach a screening disposition, find safeguards, and reconstruct evidence. |
| 2–5 s | Fair evidence conditions | arm IDs, shared evidence hash, shared output-schema hash | Conventional pack, deterministic app, and agent receive information-equivalent evidence; provider arms share the same schema. |
| 5–8 s | Traversal | retrieval rows or participant navigation events | Highlight only observed branches and tool calls. Do not animate a planned or oracle path as an observed path. |
| 8–11 s | Direct-evidence reconciliation | finding aliases, direct-run IDs, matrix/current hashes, QA | Selected findings reconnect to direct Circuitscape artifacts and accepted QA. |
| 11–14 s | Measured outcome | paired estimate, confidence interval, recall, precision, time or burden | Show only metrics present in the completed campaign and their preregistered thresholds. |
| 14–18 s | Bounded conclusion | claim ID, claim status, limitations, evidence hashes | End on `SUPPORTED`, `NOT_SUPPORTED`, or `BLOCKED` exactly as recorded in the immutable claim ledger. |

## Render gate

The renderer must refuse to start unless:

1. the source commit and working tree are verified;
2. the campaign artifact hash matches its W&B artifact version;
3. every provider row has an exact Weave trace readback;
4. each visual claim appears in the immutable claim ledger;
5. the declared result Tables contain no planned-only row used as an outcome;
6. the media byte manifest is computed before upload; and
7. checkpoint stills at 0%, 25%, 50%, 75%, and 100%, plus the poster and
   contact sheet, pass visual inspection.

Until those gates pass, the existing historical Review Brief animation remains
the only report media and must stay labelled as the historical fixed-traversal
campaign.
