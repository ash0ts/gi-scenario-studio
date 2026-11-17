# Release manifest gate

`Manifest.toml` is committed from a genuine Julia **1.12.6** resolution. It
pins Circuitscape **5.17.1** at registered UUID
`2b7a1792-8151-5239-925d-e2b8fdfa3201` and reviewed registry tree
`d612b7cc82bc4ebc176f5d0a144a484503779c39`. Do not hand-edit it or substitute
a path/repository-tracked package with matching version text.

The Python packaging entry gate in
`scripts/create_ecocorridor_engine_manifest.py` and the release workflows refuse
to build a professional engine bundle while `Manifest.toml` is absent or
resolves a different UUID, version, source tree, or tracking mode. The strict
gate lives outside the archived direct-run protocol parser so a packaging-policy
change cannot masquerade as freshly executed Ridgeway solver evidence. The Julia
verifier checks the same package identity before loading Circuitscape code.

Expected release preparation:

```text
julia-1.12.6 --project=. -e 'using Pkg; Pkg.instantiate()'
julia-1.12.6 --project=. scripts/verify_manifest.jl
julia-1.12.6 --project=. -e 'using Pkg; Pkg.precompile()'
```

Any legitimate dependency update requires a new resolved manifest, complete
vulnerability review, direct-engine tests, package rebuild, and fresh Ridgeway
producer evidence. `Pkg.resolve()` is not part of release execution because it
may rewrite the reviewed dependency graph.
