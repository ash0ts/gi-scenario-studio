module EcoCorridorEngine

using Circuitscape
using JSON3
using LinearAlgebra

const PROTOCOL_VERSION = "1.0"
const ENGINE_VERSION = "5.17.1"

function contained_path(root::String, relative::String)::String
    isabspath(relative) && error("absolute protocol path is forbidden")
    target = realpath(joinpath(root, relative))
    relative_target = relpath(target, realpath(root))
    (isabspath(relative_target) || ".." in splitpath(relative_target)) &&
        error("protocol path escapes the run directory")
    return target
end

function checked_input_path(root::String, relative::String)::String
    path = contained_path(root, relative)
    isfile(path) || error("required engine input is absent: $relative")
    islink(path) && error("symlinked engine inputs are forbidden")
    return path
end

function contained_output_path(root::String, relative::String)::String
    isabspath(relative) && error("absolute protocol output path is forbidden")
    canonical_root = realpath(root)
    normalized = normpath(relative)
    (normalized == "." || ".." in splitpath(normalized)) &&
        error("protocol output path escapes the run directory")
    target = abspath(joinpath(canonical_root, normalized))
    relative_target = relpath(target, canonical_root)
    (isabspath(relative_target) || ".." in splitpath(relative_target)) &&
        error("protocol output path escapes the run directory")
    cursor = canonical_root
    for component in splitpath(relative_target)
        cursor = joinpath(cursor, component)
        islink(cursor) && error("symlinked protocol output paths are forbidden")
        if ispath(cursor)
            resolved = realpath(cursor)
            resolved_relative = relpath(resolved, canonical_root)
            (isabspath(resolved_relative) || ".." in splitpath(resolved_relative)) &&
                error("protocol output path escapes the run directory")
        end
    end
    if ispath(target) && !isfile(target) && !isdir(target)
        error("protocol output target is not a regular file or directory")
    end
    return target
end

function parse_args(arguments)::Tuple{String,String}
    request = nothing
    result = nothing
    index = 1
    while index <= length(arguments)
        flag = arguments[index]
        index == length(arguments) && error("missing value for $flag")
        if flag == "--request"
            request = arguments[index + 1]
        elseif flag == "--result"
            result = arguments[index + 1]
        else
            error("unknown argument: $flag")
        end
        index += 2
    end
    request === nothing && error("--request is required")
    result === nothing && error("--result is required")
    return String(request), String(result)
end

function require_exact_settings(request)
    String(request.protocol_version) == PROTOCOL_VERSION || error("unsupported protocol version")
    String(request.mode) == "raster_pairwise" || error("only raster pairwise mode is supported")
    String(request.raster_semantics) == "resistance" || error("conductance is unsupported")
    settings = request.settings
    String(settings.precision) == "double" || error("only double precision is supported")
    Int(settings.julia_threads) == 1 || error("julia_threads must be one")
    Int(settings.blas_threads) == 1 || error("blas_threads must be one")
    Int(settings.omp_threads) == 1 || error("omp_threads must be one")
    neighbor_rule = String(settings.neighbor_rule)
    neighbor_rule in ("FOUR", "EIGHT") || error("unsupported neighbor_rule: $neighbor_rule")
    resistance_average = String(settings.resistance_average)
    resistance_average in ("ARITHMETIC", "HARMONIC") ||
        error("unsupported resistance_average: $resistance_average")
end

function write_ini(request, root::String)::Tuple{String,String}
    resistance = checked_input_path(root, String(request.resources.resistance))
    focal = checked_input_path(root, String(request.resources.focal))
    included_pairs = checked_input_path(root, String(request.resources.included_pairs))
    output_directory = contained_output_path(root, String(request.output_directory))
    mkpath(output_directory)
    resistance_relative = relpath(resistance, root)
    focal_relative = relpath(focal, root)
    included_pairs_relative = relpath(included_pairs, root)
    output_base = joinpath(String(request.output_directory), "connectivity")
    config_path = contained_output_path(root, "circuitscape.ini")
    four_neighbors = String(request.settings.neighbor_rule) == "FOUR"
    arithmetic = String(request.settings.resistance_average) == "ARITHMETIC"
    write_current = Bool(request.settings.emit_current_map)
    four_neighbors_value = four_neighbors ? "True" : "False"
    arithmetic_value = arithmetic ? "True" : "False"
    write_current_value = write_current ? "True" : "False"
    open(config_path, "w") do stream
        println(stream, "[Options for advanced mode]")
        println(stream, "ground_file_is_resistances = True")
        println(stream, "source_file = (Browse for a valid file)")
        println(stream, "ground_file = (Browse for a valid file)")
        println(stream, "use_unit_currents = False")
        println(stream, "use_direct_grounds = False")
        println(stream, "remove_src_or_gnd = keepall")
        println(stream, "[Mask file]")
        println(stream, "use_mask = False")
        println(stream, "mask_file = (Browse for a valid file)")
        println(stream, "[Calculation options]")
        println(stream, "low_memory_mode = False")
        println(stream, "parallelize = False")
        println(stream, "max_parallel = 1")
        println(stream, "solver = cg+amg")
        println(stream, "print_timings = False")
        println(stream, "preemptive_memory_release = True")
        println(stream, "print_rusages = False")
        println(stream, "scenario = pairwise")
        println(stream, "[Short circuit regions (aka polygons)]")
        println(stream, "use_polygons = False")
        println(stream, "polygon_file = (Browse for a valid file)")
        println(stream, "[Options for one-to-all and all-to-one modes]")
        println(stream, "use_variable_source_strengths = False")
        println(stream, "variable_source_file = (Browse for a valid file)")
        println(stream, "[Output options]")
        println(stream, "set_null_currents_to_nodata = False")
        println(stream, "set_null_voltages_to_nodata = False")
        println(stream, "compress_grids = True")
        println(stream, "write_cur_maps = $write_current_value")
        println(stream, "write_cum_cur_map_only = $write_current_value")
        println(stream, "log_transform_maps = False")
        println(stream, "write_volt_maps = False")
        println(stream, "output_file = $output_base")
        println(stream, "[Options for pairwise and one-to-all and all-to-one modes]")
        println(stream, "included_pairs_file = $included_pairs_relative")
        println(stream, "use_included_pairs = True")
        println(stream, "point_file = $focal_relative")
        println(stream, "[Connection scheme for raster habitat data]")
        println(stream, "connect_four_neighbors_only = $four_neighbors_value")
        println(stream, "connect_using_avg_resistances = $arithmetic_value")
        println(stream, "[Habitat raster or graph]")
        println(stream, "habitat_map_is_resistances = True")
        println(stream, "habitat_file = $resistance_relative")
    end
    return config_path, output_base
end

function find_matrix(output_base::String)::String
    directory = dirname(output_base)
    candidates = filter(readdir(directory; join=true)) do path
        isfile(path) && !islink(path) &&
            endswith(lowercase(basename(path)), "_resistances_3columns.out")
    end
    length(candidates) == 1 ||
        error("Circuitscape must produce exactly one effective-resistance matrix")
    return only(candidates)
end

function parse_matrix(path::String, requested_pairs)
    requested = Dict{Tuple{Int,Int},Any}()
    pair_ids = Set{String}()
    for pair in requested_pairs
        left = Int(pair.from_focal_id)
        right = Int(pair.to_focal_id)
        left > 0 && right > 0 && left != right || error("requested focal IDs are invalid")
        key = (min(left, right), max(left, right))
        pair_id = String(pair.pair_id)
        (haskey(requested, key) || pair_id in pair_ids) &&
            error("requested focal-pair identities are ambiguous")
        requested[key] = pair
        push!(pair_ids, pair_id)
    end
    values = Dict{Tuple{Int,Int},Float64}()
    observed = Set{Tuple{Int,Int}}()
    for (line_number, line) in enumerate(eachline(path))
        isempty(strip(line)) && continue
        fields = split(strip(line))
        length(fields) == 3 || error("matrix row $line_number is not exactly three columns")
        left_value = tryparse(Float64, fields[1])
        right_value = tryparse(Float64, fields[2])
        resistance = tryparse(Float64, fields[3])
        (left_value === nothing || right_value === nothing || resistance === nothing) &&
            error("matrix row $line_number is not numeric")
        (isfinite(left_value) && isfinite(right_value) && isfinite(resistance)) ||
            error("matrix row $line_number contains a nonfinite value")
        (isinteger(left_value) && isinteger(right_value)) ||
            error("matrix row $line_number has noninteger focal IDs")
        left = Int(left_value)
        right = Int(right_value)
        (left > 0 && right > 0 && left != right) ||
            error("matrix row $line_number has invalid focal IDs")
        key = (min(left, right), max(left, right))
        key in observed && error("matrix contains a duplicate focal pair")
        push!(observed, key)
        if haskey(requested, key)
            resistance > 0 || error("matrix has no positive requested-pair result")
            values[key] = resistance
        else
            resistance == -1.0 ||
                error("matrix contains an unexpected positive or undocumented result")
        end
    end
    metrics = Any[]
    for pair in requested_pairs
        left = Int(pair.from_focal_id)
        right = Int(pair.to_focal_id)
        key = (min(left, right), max(left, right))
        haskey(values, key) || error("matrix omits requested pair $(pair.pair_id)")
        push!(metrics, Dict(
            "pair_id" => String(pair.pair_id),
            "from_focal_id" => left,
            "to_focal_id" => right,
            "effective_resistance" => values[key],
        ))
    end
    return metrics
end

function relative_to_root(root::String, path::String)::String
    target = realpath(path)
    relative_target = relpath(target, realpath(root))
    (isabspath(relative_target) || ".." in splitpath(relative_target)) &&
        error("engine output escaped isolated run directory")
    return relative_target
end

function success_payload(request, root::String, matrix_path::String, pair_metrics)
    current_path = nothing
    if Bool(request.settings.emit_current_map)
        candidates = filter(readdir(dirname(matrix_path); join=true)) do path
            isfile(path) && !islink(path) && occursin("cum_curmap", lowercase(basename(path)))
        end
        length(candidates) == 1 ||
            error("requested run must produce exactly one cumulative current map")
        current_path = relative_to_root(root, only(candidates))
    end
    return Dict(
        "protocol_version" => PROTOCOL_VERSION,
        "status" => "SUCCEEDED",
        "engine_name" => "Circuitscape",
        "circuitscape_version" => ENGINE_VERSION,
        "pair_metrics" => pair_metrics,
        "matrix_path" => relative_to_root(root, matrix_path),
        "current_map_path" => current_path,
        "review_mask_summaries" => Any[],
        "warnings" => Any[],
        "failure_message" => nothing,
    )
end

function failure_payload(message::String)
    return Dict(
        "protocol_version" => PROTOCOL_VERSION,
        "status" => "FAILED",
        "engine_name" => "Circuitscape",
        "circuitscape_version" => ENGINE_VERSION,
        "pair_metrics" => Any[],
        "matrix_path" => nothing,
        "current_map_path" => nothing,
        "review_mask_summaries" => Any[],
        "warnings" => Any[],
        "failure_message" => message,
    )
end

function run(request_path::String, result_path::String)::Int
    root = pwd()
    request_absolute = checked_input_path(root, request_path)
    contained_output_path(root, result_path)
    try
        BLAS.set_num_threads(1)
        request = JSON3.read(read(request_absolute, String))
        require_exact_settings(request)
        config_path, output_base = write_ini(request, root)
        contained_output_path(root, String(request.output_directory))
        contained_output_path(root, "circuitscape.ini") == config_path ||
            error("effective configuration path changed")
        Circuitscape.compute(config_path)
        contained_output_path(root, String(request.output_directory))
        contained_output_path(root, "circuitscape.ini") == config_path ||
            error("effective configuration path changed during solve")
        matrix_path = find_matrix(output_base)
        relative_to_root(root, matrix_path)
        pair_metrics = parse_matrix(matrix_path, request.pairs)
        payload = success_payload(request, root, matrix_path, pair_metrics)
        if payload["current_map_path"] !== nothing
            contained_path(root, String(payload["current_map_path"]))
        end
        result_absolute = contained_output_path(root, result_path)
        write(result_absolute, JSON3.write(payload))
        return 0
    catch error
        message = sprint(showerror, error)
        result_absolute = contained_output_path(root, result_path)
        write(result_absolute, JSON3.write(failure_payload(message)))
        return 1
    end
end

Base.@ccallable function julia_main()::Cint
    try
        request_path, result_path = parse_args(ARGS)
        return Cint(run(request_path, result_path))
    catch error
        Base.display_error(Base.catch_stack())
        return Cint(2)
    end
end

end
