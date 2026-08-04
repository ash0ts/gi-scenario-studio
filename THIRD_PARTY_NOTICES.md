# Third-party notices

The source tree is Apache-2.0. Dependencies and datasets retain their own
licenses. The release build must generate a complete SBOM and license report.

## EPA SWMM

EPA's Storm Water Management Model source is public domain. GI Scenario Studio
records the exact solver binary hash used for each analysis. The application is
not an EPA product and does not imply EPA review or approval.

## SWMManywhere

The optional concept-model adapter targets
[SWMManywhere](https://github.com/ImperialCollegeLondon/SWMManywhere), which is
licensed BSD-3-Clause. Synthesized networks are conceptual and must be reviewed.

## Circuitscape

EcoCorridor Scenario Studio uses the explicitly packaged
[Circuitscape.jl](https://github.com/Circuitscape/Circuitscape.jl) engine under
the MIT License. Circuitscape is not an animal-movement, collision, population,
or regulatory model. Each reportable connectivity value records the exact
engine and Julia runtime hashes used by its direct run.

## Julia and PackageCompiler

The target-specific EcoCorridor engine bundle includes Julia and is assembled
with PackageCompiler. Both are MIT-licensed. The release process must retain
their license texts and the complete Julia package/artifact inventory; this
notice is not a substitute for that generated inventory.

## W&B Models and Weave

W&B Models and Weave are optional SDK/runtime dependencies of the professional
observability path and retain their respective licenses. They are cloud lineage
and trace services, not ecological or engineering authorities.

## Benchmark assets

Benchmark data is never redistributed merely because it is publicly visible.
Each benchmark manifest records its license, source, version, checksum,
attribution, modifications, and redistribution decision.
