# Scientific and environmental claim contract

## Supported model

Version 0.1 supports only raster pairwise Circuitscape projects with resistance
semantics. Conductance, network, advanced, one-to-all, and all-to-one modes fail
with `UNSUPPORTED_FEATURE`.

Each ecological scenario fixes:

- focal guild or life stage and focal-area set;
- one reviewed resistance hypothesis;
- future condition and intervention maturity horizon;
- included focal pairs and safeguard status;
- exact aligned grid identity; and
- precision, four/eight-neighbor choice, resistance averaging, and thread
  settings.

Raw effective resistance is never pooled across incompatible scenarios. The
decision board uses within-scenario baseline-relative change for every required
focal pair. A portfolio is eligible for comparison only after all required
target and safeguard jobs succeed.

## Intervention semantics

An option is a reviewer-approved complete footprint plus scenario-specific
absolute replacement resistance cells. Cells outside the footprint are
unchanged. Effects cannot change grid topology or NODATA. Separately selectable
footprints cannot overlap; an intentional combined effect must be authored and
reviewed as one composite option.

Unknown or unreviewed effects are not inferred by AI. `SURVEY_FIRST` preserves
the feasibility decision while sharing unchanged baseline connectivity where
appropriate.

## Permitted statements

- “Direct Circuitscape result under documented assumptions.”
- “Nondominated among directly evaluated portfolios.”
- “Candidate to carry forward for ecological, field, and engineering
  feasibility review.”

## Prohibited statements

The UI, exports, and assistants must not claim observed or predicted animal
movement, crossing use, collision reduction, population or biodiversity
benefit, an optimal corridor, regulatory approval, or design readiness.
Current-density rasters are diagnostic electrical-current visualizations, not
objectives or animal-use predictions.

## Evidence resolution

```text
Claim → rule/reducer → direct run → ecological scenario
→ intervention raster diff → resistance and focal assets
→ engine manifest → QA → assumptions and data authorization
```

Every displayed and exported number carries a stable evidence claim ID. Missing
nodes, failed QA, incomplete scenario ensembles, or hash mismatches make the
claim unavailable rather than estimated.
