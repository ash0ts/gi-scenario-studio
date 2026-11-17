fn main() {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if arguments
        .first()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value == "--release-health-check")
    {
        if arguments.len() != 2
            || ecocorridor_scenario_studio_desktop_lib::run_release_health_check(
                std::path::Path::new(&arguments[1]),
            )
            .is_err()
        {
            eprintln!("EcoCorridor installed-host health check failed closed.");
            std::process::exit(78);
        }
        return;
    }
    ecocorridor_scenario_studio_desktop_lib::run();
}
