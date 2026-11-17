# Report-film analysis contract

## Audience and question

- **Audience:** environmental and stormwater engineers, students, and curious
  readers with no assumed knowledge of SWMM or multiobjective search.
- **Question:** How can modeled evidence help narrow GI alternatives without
  hiding an adverse storm response or overstating ecological impact?
- **Takeaway:** direct SWMM results can focus engineering investigation; search
  allocates simulation attention, and the evidence boundary remains visible.
- **Out of scope:** field validity, annual performance, calibration, regulatory
  compliance, construction readiness, real cost, water quality, habitat,
  groundwater recharge, avoided damages, energy, carbon, or a general search
  winner.

## Analytical structure

- **Fixed:** Cedar Trace model, four synthetic storms, SWMM version, LID
  definitions, decision rules, and synthetic cost basis.
- **Varied:** four bounded integer LID counts and, in the benchmark, the search
  method, seed, and exact-job-equivalent budget.
- **Measured in the fixture:** modeled outlet volume and peak, SWMM runoff
  accounting, numerical QA, synthetic cost, exact-run-equivalent accounting,
  and search recovery metrics.
- **Observed:** ALT-017 is attractive on two axes but fails two required rules;
  ALT-207 changes the synthetic long-event water ledger; the exhaustive study
  leaves twelve complete-rule alternatives; the adaptive proposer does not
  pass its release gate.
- **Interpretation:** a smaller, inspectable shortlist can focus costly field
  work without becoming a design verdict.
- **Limitations:** one synthetic network, four synthetic storms, fictional
  costs, no calibration or field observation, no measured compute energy.
- **Next question:** which candidates survive local storm selection, model
  review, soils, utilities, maintenance, constructability, water-quality, and
  lifecycle assessment?

## Color and motion roles

- Rain blue: fixed rainfall and modeled water.
- Channel teal: evidence-supported passage through a declared rule.
- Flood red: an observed adverse result or failed release condition.
- Caution ochre: assumptions, limitations, or unanswered evidence.
- Watershed ink and survey paper: neutral structure.
- The recurring water/evidence packet preserves identity when a quantity moves
  from model response to plot, storm cell, search request, and evidence record.

