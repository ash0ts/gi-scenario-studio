# Product principles

## Exact means simulator-exact, not real-world truth

An exact result is produced by the pinned SWMM engine from documented inputs.
It does not imply that the network, rainfall, LID, cost, or boundary assumptions
perfectly represent the physical system.

## Readiness is a human-controlled state

`DEMO`, `CONCEPTUAL`, `ENGINEER_REVIEWED`, and `REFERENCE_MATCHED` are explicit
project states. The application can present evidence for promotion but cannot
promote a project on its own.

## AI allocates compute

The learned proposer may rank unevaluated candidates. It cannot author physical
parameters, declare compliance, or provide a reportable hydraulic value.

## Every result carries a path back to evidence

The Evidence Rail is both a UX pattern and a system invariant. A result that
cannot resolve its solver job, inputs, QA, and assumptions is not a result.

## Open source is a trust mechanism

The core, schemas, benchmark methodology, and evidence exports are open. Paid
services may support deployment and validation, but traceability is never a
paywalled capability.
