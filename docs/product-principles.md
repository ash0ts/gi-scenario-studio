# Product principles

## Exact means simulator-exact, not real-world truth

An exact result is produced by the pinned SWMM engine from documented inputs.
It does not imply that the network, rainfall, LID, cost, or boundary assumptions
perfectly represent the physical system.

## Fitness is specific to a model revision and intended use

Project kind (`demo`, `conceptual`, or `existing`) records where a project came
from; it is not a maturity grade. A separate Model Fitness Passport records the
intended use, criterion-level evidence, permitted outputs, prohibited uses,
unresolved limitations, and an engineer's review for one model revision.

Passport dispositions are `not_assessed`, `fit_for_stated_use`,
`fit_with_limitations`, and `not_fit`. They are not an ordinal ladder and the
application cannot advance one automatically. Numerical QA, model fitness, W&B
synchronization, and professional acceptance remain independent facts.

## AI allocates compute

The learned proposer may rank unevaluated candidates. It cannot author physical
parameters, declare compliance, or provide a reportable hydraulic value.

## Every result carries a path back to evidence

The Evidence Rail is both a UX pattern and a system invariant. A result that
cannot resolve its solver job, inputs, QA, and assumptions is not a result.

## W&B is required, but it is not engineering authority

Every production project is bound to a user-owned W&B project whose visibility
is verified as `Restricted`. W&B Models and Weave mirror computation, evidence,
lineage, and assistant traces; local SQLite state and content-addressed bytes
remain the authoritative engineering record. After the initial verified
binding, an outage may queue synchronization but cannot change SWMM inputs,
candidate order, exact results, QA, costs, or export content.

## Open source is a trust mechanism

The core, schemas, benchmark methodology, and evidence exports are open. Paid
services may support deployment and validation, but traceability is never a
paywalled capability.
