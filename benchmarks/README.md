# Benchmark policy

The benchmark suite separates code, model, and dataset rights. Each asset lives
behind a manifest containing:

- SPDX license identifier;
- source URL or DOI;
- version and checksum;
- attribution and modification notice;
- whether redistribution is permitted;
- readiness and engineering limitations.

The in-repository `cc0_reference` fixture is authored for this project and may
be redistributed as CC0. External assets are fetched only when their terms have
been reviewed.

`evidence_guide/v1` is a separate 48-case synthetic CC0 evaluation dataset for
the explanation layer. It contains no hydraulic oracle or customer content and
does not evaluate field validity. Its pinned manifest and deterministic scorer
test evidence citation, exact values and units, limitations, claim boundaries,
read-only authority, and safe refusal.
Its candidate runner and two failed W&B Inference evaluations are preserved
unchanged as historical protocol-regression evidence. v1 is not used to enable
the product because its provider packet exposed expected intent/answer content,
its tool spans were not causally grounded, and its numeric checks did not cover
spelled-out hallucinations. See `evidence_guide/v1/README.md` for its exact
receipt format.

`evidence_guide/v2` is the architecture-first explanation benchmark. It contains
120 visible development scenarios and 120 frozen validation scenarios with
project-family and question-template separation. The checked-in hidden receipt
pins a separately stored 300-scenario lockbox; hidden rows are deliberately
Git-ignored. Each provider attempt shuffles claims and remaps opaque IDs, strips
trusted targets and gold plans, and withholds causal targets until a bounded
read-only tool executes. The local scorer covers digit and spelled-out values,
units, signs, rounding, evidence identity/freshness/subject, limitations,
actions, injections, overclaims, and paired tool-result counterfactuals. The
campaign measures automated conformance and automated comparative explanation
quality only; practitioner usefulness remains `not_measured`.

`realistic_fehraltorf/v1` is an independent reanalysis of the CC0 Eawag
Fehraltorf study's published thirty-year SWMM output tables and retained RPT
files. It verifies the public archive hashes, recomputes paired-year annual CSO
reductions using the authors' method, and keeps execution-QA findings beside
the result. It is not a fresh SWMM rerun because the separately licensed
meteorological forcing is not redistributed, and it is not field validation.
