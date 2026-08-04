using PackageCompiler

root = normpath(joinpath(@__DIR__, ".."))
project = joinpath(root, "julia", "EcoCorridorEngine")
output = get(ENV, "ECOCORRIDOR_ENGINE_OUTPUT", "")
isempty(output) && error("ECOCORRIDOR_ENGINE_OUTPUT is required")

create_app(
    project,
    abspath(output);
    executables = ["ecocorridor-engine" => "julia_main"],
    force = false,
    include_lazy_artifacts = true,
    filter_stdlibs = false,
)
