# Research basis and claim boundaries

GI Scenario Studio is deliberately narrower than a general stormwater design
platform. The category already includes public and commercial planning tools;
the product differentiates through a user-owned Restricted W&B evidence mirror,
local engineering authority, transparent exact execution, open schemas, and
evidence that can be independently rerun.

## Primary references

- [EPA SWMM](https://www.epa.gov/water-research/storm-water-management-model-swmm)
  defines the supported public-domain simulator and eight native LID types.
- [SWMM 5.2 user manual](https://www.epa.gov/system/files/documents/2022-04/swmm-users-manual-version-5.2.pdf)
  is normative for input, LID deployment, numerical diagnostics, and reports.
- [EPA GI modeling toolkit](https://www.epa.gov/water-research/green-infrastructure-modeling-toolkit)
  shows that optimization and life-cycle comparison are established categories.
- [EPA CSO LTCP review](https://www.epa.gov/system/files/documents/2022-06/LTCP%20Review%20Fact%20Sheet.pdf)
  supports alternatives and cost/performance analysis in that specific context;
  the application does not generalize this into a universal MS4 mandate.
- [NOAA Atlas 15](https://water.noaa.gov/about/atlas15) is tracked as a data
  source, not treated as an adopted standard before final publication.
- [SWMManywhere](https://github.com/ImperialCollegeLondon/SWMManywhere) is the
  optional BSD-licensed concept-model synthesis adapter. Its output retains
  `project_kind: conceptual`; a separate Model Fitness Passport remains
  `not_assessed` until an engineer reviews the stated-use evidence.

## Claims allowed in product copy

- Exact SWMM result under documented assumptions.
- Nondominated among exact alternatives evaluated.
- Tested against the selected storm cases.
- Conceptual model synthesized from documented source assets.

## Claims prohibited in product copy

- Real-world truth.
- Verified global optimum unless a finite candidate universe was exhaustively run.
- Calibrated, regulator approved, filing ready, or guaranteed resilient.
- Measured energy when the method is a TDP-based estimate.
- Model confidence when an interval only describes approximation error.
