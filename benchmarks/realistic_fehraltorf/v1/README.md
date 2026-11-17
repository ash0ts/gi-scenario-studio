# Fehraltorf published-output reanalysis

`case-study.json` is a deterministic, credential-free reanalysis of the public
Eawag dataset supporting Rodriguez, Cavadini, and Cook (2024), *Do baseline
assumptions alter the efficacy of green stormwater infrastructure to reduce
combined sewer overflows?*

This is a realistic public catchment case, but it is **not a fresh SWMM
rerun**. The source package does not include complete `.out` files, and a fresh
run requires MeteoSwiss rainfall and evapotranspiration data that the authors
could not redistribute. The builder therefore verifies the public input and
result archive hashes, recomputes the paper's annual CSO summaries from the
published result CSVs, and extracts QA evidence from all 20 retained SWMM
reports.

The calculation follows the authors' published notebook:

1. Set `System` values below `0.003 L/s` to zero.
2. Compute annual mean flow and convert it with a 365-day year to `m3/year`.
3. For each model structure and GI scenario, calculate
   `median(baseline annual volume − GI annual volume) / median(baseline annual
   volume) × 100`, pairing values by calendar year.

The source archive and every contributing CSV/RPT member are SHA-256 bound in
the JSON. No downloaded archive is committed.

## Rebuild

Download the three resources identified in `case-study.json`, then run:

```bash
PYTHONPATH=python python scripts/build_fehraltorf_case_study.py \
  --input-archive /path/to/swmm_input_files.zip \
  --results-archive /path/to/results.zip \
  --readme /path/to/README.md \
  --output benchmarks/realistic_fehraltorf/v1/case-study.json
```

The command fails closed if any public-source hash changes. The checked-in JSON
contains only public URLs, hashes, derived values, QA fields, and explicit claim
boundaries—never local paths, credentials, or runtime metadata.

## Interpretation boundary

The case demonstrates that modeled GI performance can change substantially
with model structure and that a favorable percentage must be read alongside
solver QA. It does not establish field performance, regulatory suitability,
construction feasibility, water-quality benefit, or ecological benefit.

Sources: [dataset](https://doi.org/10.25678/0009PE),
[paper](https://doi.org/10.1016/j.watres.2024.121284).
