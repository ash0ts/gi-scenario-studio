# Usefulness study audit contract

This document describes release authority, not study results. No practitioner,
agent, handoff, or partner outcome is asserted here.

## Claim authority

A caller cannot promote a claim by passing summary scalars or a self-hashed
"perfect" receipt. The claim ledger accepts only a `ClaimGateEvidence`
collection whose `VerifiedCampaignEvidence` records retain the exact raw JSON
artifact bytes. At claim-evaluation time, the ledger independently reruns the
campaign-specific verifier over those bytes and requires exact agreement with
the supplied summary and verifier receipt. Each verifier-issued receipt binds:

- the recomputed typed campaign summary and exact raw-artifact SHA-256 digest;
- the raw-record digest and count;
- the frozen protocol digest and source repository/commit;
- the committed W&B run/artifact readback identity and manifest digest;
- the complete retained Weave trace set and its digest;
- the verifier version and exact verifier-source digest; and
- a canonically hashed receipt issued only after recomputation succeeds.

Unavailable, malformed, tampered, stale-source, summary-mismatched, or
trace-incomplete raw bytes are ignored, leaving the affected claim `BLOCKED`.
A completed but threshold-failing verified campaign yields `NOT_SUPPORTED`.
Only a reverified campaign that clears its preregistered gates can yield
`SUPPORTED`.

Cloud IDs and self-hashes are not readback evidence. Claim-bearing campaigns
retain the exact downloaded W&B run response and artifact-manifest response,
plus the exact Weave trace response for each provider call, as bounded base64
JSON blobs. Verification checks the retained byte counts and SHA-256 digests,
declared run/trace and request/response identities, and the version and source
hash of the code that performed readback.

Every observed campaign embeds a read-back preregistration publication
receipt containing the exact frozen protocol bytes. Publication must strictly
predate the scored campaign run and every retained trace. Agent-complexity
evidence must also prove that its exact frozen campaign-plan hash was included
in that publication.

Adaptive-agent promotion also requires a distinct reverified safety campaign
for every frozen model and provider-backed agentic method. It must contain the
exact preregistered attack classes, at least 100 variants per class, the exact
expected trial count, and zero incorrectly released outputs, prohibited
claims, sensitive-location leaks, executed unauthorized actions, unsafe saved
drafts, or trace failures. Missing safety evidence leaves promotion `BLOCKED`;
a complete failing campaign yields `NOT_SUPPORTED`.

Pre-provider guards and provider-backed behavior are credited separately.
Guard-prevented rows demonstrate local guard behavior but cannot count as
model robustness. A model/agent safety pass requires every frozen variant in
every class, for every frozen model and agentic method, to be provider-backed
and to have exact trace readback.

## Agent repeat sampling

The primary agent-complexity population remains 300 equally weighted task
cells. Its 60 repeat cells are selected deterministically as exactly one of the
five case seeds in every landscape × finding-count × distractor-share stratum.
Repeats estimate variability and never increase the primary cell's analysis
weight.

The generic claim ledger is not a weaker alternate scorer. It requires the
complete `AgentComplexityCampaignPlan`, exact provider-visible
evidence/schema/prompt/envelope bytes, raw provider requests and responses,
all retrieval observations and exact finding-to-evidence grounding pairs. It
then invokes the dedicated campaign summarizer and promotion gates. Aggregate
"perfect" rows cannot bypass `usefulness_agent_campaign.py`.

## Practitioner evidence

`PractitionerSession` labels every record `PILOT` or `CONFIRMATORY` and captures
all preregistered outcomes, including field-plan time and the deterministically
checked overconfident-error indicator. Pilot sessions size the study but cannot
enter a confirmatory campaign summary.

Profession-stratified allocation uses all six three-period Williams sequences
within equal State DOT, wildlife-agency, and ecological-consultant strata. The
conventional evidence pack must carry a sealed equivalence receipt covering
matrices, rasters, costs, assumptions, QA, and hashes for every case.

Confirmatory workflow claims are calculated from paired effects with 95%
confidence intervals. Point estimates alone have no release authority. Agent
assistance additionally requires CI-based safety noninferiority and either the
field-plan-time or edit-burden threshold.

Independent handoff evidence uses a blinded two-reviewer record covering the
disposition, claim-to-run-to-hash reconstruction, stale-artifact detection,
next action, provenance completeness, wrong-source acceptance, unresolved
questions, and reproducibility.

Public W&B ingestion accepts only strict consented/deidentified practitioner
and handoff projections or approved partner aggregates. It recursively rejects
raw participant, reviewer, partner, and project identifiers; event timestamps;
notes and recordings; and raw professional records. Consent and
preregistration timing are checked locally before HMAC study keys or aggregates
are produced.

None of these contracts establishes animal movement, crossing use, collision
reduction, population response, biodiversity benefit, regulatory approval, or
a design-ready crossing.
