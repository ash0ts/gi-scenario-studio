# Report-film claim ledger

Every numerical value displayed by the Canvas runtime is projected into
`data/animation-data.js` by `scripts/build_animation_data.py`. Rendering fails
when a source hash or expected claim drifts.

| Claim ID | Approved visible claim | Source | Required boundary |
|---|---|---|---|
| `A-ALT017` | ALT-017 costs $54.99k, reduces aggregate outlet volume 4.14%, misses the 6% target, and raises two peaks: 3.14→3.21 cfs and 13.33→13.34 cfs. | `guidedFixture.json`, ALT-017 screening and storm runs | Synthetic teaching evidence; nondominated on two axes is not acceptable under every rule. |
| `A-WATER-LEDGER` | In the long synthetic event, precipitation is 0.937 acre-ft; baseline→ALT-207 runoff terms are infiltration loss 0.412→0.436, surface runoff 0.514→0.447, final storage 0.012→0.043, and LID drainage 0→0.014 acre-ft. | Retained baseline and ALT-207 SWMM RPT files | SWMM runoff-accounting terms, modeled not measured; infiltration loss is not groundwater recharge. |
| `A-OUTFALL` | Long-event external outflow is 0.167→0.150 Mgal; across four separate synthetic tests, OUT1 volume is 0.748→0.700 Mgal, approximately 48,000 fewer modeled gallons. | RPT routing ledger and guided fixture | Separate events, not annualized; no real flood or water-quality claim. |
| `A-STORMS` | Positive reported peak deltas occur for 1, 0, 420, and 47 alternatives by selected storm, with 421 unique adverse alternatives and 47 failing twice. | `storm_results.csv` and exact dataset | Storm depth, duration, and temporal shape changed together; no isolated causal factor. |
| `A-FUNNEL` | 699 exact alternatives narrow to 297 volume-pass, 46 volume-and-cost-pass, 12 complete-rule-pass, and four exact feasible-frontier alternatives; QA removes none. | `exact_alternatives.csv` and exact dataset | Exact only inside this finite synthetic universe. |
| `A-SEARCH` | At 256 designs, ExtraTrees has median hypervolume 1.000, feasible recall 83.3%, and adverse recall 46.2%; it passes one of five release gates. | campaign aggregates and release-gate CSV | One synthetic network and ten seeds; no universal method ranking. |
| `A-GATE` | 95% of 421 adverse alternatives requires at least 400 requests, but the declared budget permits 256; the theoretical recall ceiling is 60.8%. | release-gate audit and exact counts | The gate is defective; this is not evidence for or against ExtraTrees. |
| `A-COMPUTE` | kWh and CO2e were not measured. | benchmark manifest and research basis | Exact-job equivalents are accounting units, not energy or carbon savings. |

Persistent numerical-film notice:

> Synthetic test events · modeled, not measured · not annualized · not field evidence.

