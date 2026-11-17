using Pkg
using UUIDs

VERSION == v"1.12.6" || error("EcoCorridor releases require Julia 1.12.6; found $(VERSION)")
isfile(joinpath(@__DIR__, "..", "Manifest.toml")) || error("Manifest.toml is required")

dependencies = Pkg.dependencies()
circuitscape_uuid = UUID("2b7a1792-8151-5239-925d-e2b8fdfa3201")
circuitscape = get(dependencies, circuitscape_uuid, nothing)
circuitscape === nothing && error("The registered Circuitscape package UUID is absent")
circuitscape.name == "Circuitscape" ||
    error("The registered Circuitscape UUID resolved to $(circuitscape.name)")
circuitscape.version == v"5.17.1" ||
    error("Circuitscape 5.17.1 is required; resolved $(circuitscape.version)")
circuitscape.is_tracking_path && error("Circuitscape path tracking is forbidden")
circuitscape.is_tracking_repo && error("Circuitscape repository tracking is forbidden")
reviewed_tree = "d612b7cc82bc4ebc176f5d0a144a484503779c39"
circuitscape.tree_hash !== nothing || error("Circuitscape registry tree hash is absent")
string(circuitscape.tree_hash) == reviewed_tree ||
    error("Circuitscape registry tree hash is not the reviewed source tree")

# Load package code only after its registered identity and source tree pass.
using Circuitscape
loaded_version = pkgversion(Circuitscape)
loaded_version == circuitscape.version ||
    error("Resolved Circuitscape $(circuitscape.version), but loaded $(loaded_version)")

println("Verified Julia 1.12.6 and loaded Circuitscape $(loaded_version)")
