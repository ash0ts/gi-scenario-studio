using EcoCorridorEngine
using Circuitscape
using JSON3
using Test

@testset "runtime package identity" begin
    @test pkgversion(Circuitscape) == v"5.17.1"
    @test EcoCorridorEngine.ENGINE_VERSION == string(pkgversion(Circuitscape))
end

function request_with(; neighbor_rule="EIGHT", resistance_average="ARITHMETIC")
    return JSON3.read(
        JSON3.write(
            Dict(
                "protocol_version" => "1.0",
                "mode" => "raster_pairwise",
                "raster_semantics" => "resistance",
                "settings" => Dict(
                    "precision" => "double",
                    "julia_threads" => 1,
                    "blas_threads" => 1,
                    "omp_threads" => 1,
                    "neighbor_rule" => neighbor_rule,
                    "resistance_average" => resistance_average,
                ),
            ),
        ),
    )
end

@testset "strict protocol settings" begin
    @test_nowarn EcoCorridorEngine.require_exact_settings(request_with())
    @test_throws ErrorException EcoCorridorEngine.require_exact_settings(
        request_with(neighbor_rule="SIX"),
    )
    @test_throws ErrorException EcoCorridorEngine.require_exact_settings(
        request_with(resistance_average="GEOMETRIC"),
    )
end

@testset "Circuitscape three-column matrix dialect" begin
    mktemp() do path, stream
        write(stream, "1.0\t2.0\t9.25\n1.0\t3.0\t-1.0\n")
        close(stream)
        requested = JSON3.read(
            "[{\"pair_id\":\"pair_1_2\",\"from_focal_id\":1,\"to_focal_id\":2}]",
        )
        metrics = EcoCorridorEngine.parse_matrix(path, requested)
        @test length(metrics) == 1
        @test metrics[1]["pair_id"] == "pair_1_2"
        @test metrics[1]["effective_resistance"] == 9.25
    end
end

function write_ascii(path, values; nodata=-9999)
    rows, columns = size(values)
    open(path, "w") do stream
        println(stream, "ncols $columns")
        println(stream, "nrows $rows")
        println(stream, "xllcorner 0")
        println(stream, "yllcorner 0")
        println(stream, "cellsize 1")
        println(stream, "NODATA_value $nodata")
        for row in axes(values, 1)
            println(stream, join(values[row, :], " "))
        end
    end
end

function run_toy_landscape(resistance, focal)
    mktempdir() do directory
        write_ascii(joinpath(directory, "resistance.asc"), resistance)
        write_ascii(joinpath(directory, "focal.asc"), focal; nodata=0)
        write(joinpath(directory, "pairs.tsv"), "mode\tinclude\n1\t2\n")
        request = Dict(
            "protocol_version" => "1.0",
            "mode" => "raster_pairwise",
            "raster_semantics" => "resistance",
            "resources" => Dict(
                "resistance" => "resistance.asc",
                "focal" => "focal.asc",
                "included_pairs" => "pairs.tsv",
            ),
            "output_directory" => "outputs",
            "settings" => Dict(
                "precision" => "double",
                "julia_threads" => 1,
                "blas_threads" => 1,
                "omp_threads" => 1,
                "neighbor_rule" => "FOUR",
                "resistance_average" => "ARITHMETIC",
                "emit_current_map" => false,
            ),
            "pairs" => [
                Dict(
                    "pair_id" => "pair_1_2",
                    "from_focal_id" => 1,
                    "to_focal_id" => 2,
                ),
            ],
        )
        write(joinpath(directory, "request.json"), JSON3.write(request))
        previous = pwd()
        try
            cd(directory)
            @test EcoCorridorEngine.run("request.json", "result.json") == 0
            result = JSON3.read(read("result.json", String))
            @test String(result.status) == "SUCCEEDED"
            return Float64(result.pair_metrics[1].effective_resistance)
        finally
            cd(previous)
        end
    end
end

@testset "direct Circuitscape hand-calculated landscapes" begin
    # Three two-ohm cells form two series edges between the focal endpoints.
    series = run_toy_landscape(
        [-9999.0 -9999.0 -9999.0; 2.0 2.0 2.0; -9999.0 -9999.0 -9999.0],
        [0 0 0; 1 0 2; 0 0 0],
    )
    @test series ≈ 4.0 atol = 1.0e-8

    # Two identical two-ohm edges run in parallel between shorted focal regions.
    parallel = run_toy_landscape(
        [-9999.0 -9999.0; 2.0 2.0; 2.0 2.0; -9999.0 -9999.0],
        [0 0; 1 2; 1 2; 0 0],
    )
    @test parallel ≈ 1.0 atol = 1.0e-8
end

@testset "official Circuitscape raster-pairwise fixture" begin
    package_root = dirname(dirname(pathof(Circuitscape)))
    fixture_root = joinpath(package_root, "test", "input", "raster", "pairwise", "4")
    expected_path = joinpath(
        package_root,
        "test",
        "output_verify",
        "sgVerify4_resistances_3columns.out",
    )
    @test isdir(fixture_root)
    @test isfile(expected_path)
    mktempdir() do directory
        input = joinpath(directory, "input", "raster", "pairwise", "4")
        mkpath(input)
        mkpath(joinpath(directory, "output"))
        for name in ("cellmap.asc", "points.asc", "sgVerify4.ini")
            cp(joinpath(fixture_root, name), joinpath(input, name))
        end
        previous = pwd()
        try
            cd(directory)
            Circuitscape.compute(joinpath("input", "raster", "pairwise", "4", "sgVerify4.ini"))
        finally
            cd(previous)
        end
        observed_path = joinpath(directory, "output", "sgVerify4_resistances_3columns.out")
        positive_pairs = Any[]
        for line in eachline(expected_path)
            fields = split(strip(line))
            isempty(fields) && continue
            length(fields) == 3 || error("official fixture row is malformed")
            left_value = parse(Float64, fields[1])
            right_value = parse(Float64, fields[2])
            isinteger(left_value) && isinteger(right_value) ||
                error("official fixture focal IDs are noninteger")
            left = Int(left_value)
            right = Int(right_value)
            resistance = parse(Float64, fields[3])
            resistance > 0 || continue
            push!(positive_pairs, Dict(
                "pair_id" => "pair_$(min(left, right))_$(max(left, right))",
                "from_focal_id" => left,
                "to_focal_id" => right,
            ))
        end
        requested = JSON3.read(JSON3.write(positive_pairs))
        observed = EcoCorridorEngine.parse_matrix(observed_path, requested)
        expected = EcoCorridorEngine.parse_matrix(expected_path, requested)
        # The official expected file is decimal-rounded to eight places.
        @test length(observed) == length(expected) == length(positive_pairs)
        @test Set(item["pair_id"] for item in observed) == Set(
            item["pair_id"] for item in expected
        )
        for (observed_metric, expected_metric) in zip(observed, expected)
            @test observed_metric["pair_id"] == expected_metric["pair_id"]
            @test observed_metric["effective_resistance"] ≈
                expected_metric["effective_resistance"] atol = 5.0e-8
        end
    end
end
