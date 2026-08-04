# Release manifest gate

`Manifest.toml` is intentionally not fabricated in this source tree. The build
environment used for a release must run Julia **1.12.6**, instantiate this
project, and commit the generated manifest only after verifying that it resolves
Circuitscape **5.17.1** and the audited native artifacts.

The Python `build_engine_manifest` release gate and packaging checks refuse to
build a professional engine bundle while `Manifest.toml` is absent or resolves a
different Circuitscape version. Development code must not replace this gate with
an empty, hand-authored, or host-specific manifest.

Expected release preparation:

```text
julia-1.12.6 --project=. -e 'using Pkg; Pkg.instantiate(); Pkg.resolve()'
julia-1.12.6 --project=. scripts/verify_manifest.jl
```
