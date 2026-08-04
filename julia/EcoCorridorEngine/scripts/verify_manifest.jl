using Pkg

VERSION == v"1.12.6" || error("EcoCorridor releases require Julia 1.12.6; found $(VERSION)")
isfile(joinpath(@__DIR__, "..", "Manifest.toml")) || error("Manifest.toml is required")

dependencies = Pkg.dependencies()
circuitscape = only(filter(pair -> pair.second.name == "Circuitscape", collect(dependencies)))
circuitscape.second.version == v"5.17.1" ||
    error("Circuitscape 5.17.1 is required; found $(circuitscape.second.version)")

println("Verified Julia 1.12.6 and Circuitscape 5.17.1")
