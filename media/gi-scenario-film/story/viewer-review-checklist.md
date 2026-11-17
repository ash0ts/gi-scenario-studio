# First-time viewer review

Use this after assembling the animation inserts with the application recording.
The reviewer should not read the scripts or claim ledger first.

## Session record

- Reviewer:
- Date:
- Cut identifier or hash:
- Runtime:
- Viewing conditions:

## Unprompted explanation

Ask the reviewer to explain the film in their own words before showing the
questions below. Record their language verbatim.

## Required outcomes

For each prompt, mark **clear**, **partly clear**, or **not clear**, then record
the answer. A passing review requires all five answers to be clear without the
facilitator supplying the distinction.

1. How are runoff volume and peak flow different?
2. What does a direct SWMM result establish, and what does it not establish?
3. What does search or ML choose, and where do the displayed hydraulic values
   come from?
4. How can `ALT-017` be nondominated on the tradeoff plot and still fail the
   decision brief?
5. What is the strongest conclusion the film permits about `ALT-207`?

Expected boundaries:

- Volume is an event total; peak is the highest modeled rate. Aggregating volume
  can hide a storm-specific peak increase.
- SWMM computes an exact modeled response for recorded inputs and assumptions;
  it does not prove field truth, calibration, compliance, or constructability.
- Search selects the next bounded candidate to evaluate. A direct SWMM run—not
  ML—supplies every displayed hydraulic result.
- Pareto status describes the plotted objectives. `ALT-017` misses the volume
  target and violates the selected-storm peak guardrail.
- `ALT-207` is only a candidate to carry forward for site and feasibility review.

## Usability observations

- Where did the viewer pause or rewind?
- Which label, transition, chart, or application control was unclear?
- Could the viewer distinguish verified replay from a new local rerun?
- Could the viewer find the run, storm, mutation, files, hashes, QA, assumptions,
  and limitations behind the `+0.07 cfs` claim?
- Did any wording imply that W&B, Weave, search, or the application was a
  hydraulic or engineering authority?

## Disposition

- [ ] Pass: all five required outcomes were clear.
- [ ] Revise: record the exact timestamp and required correction below.

Notes:

