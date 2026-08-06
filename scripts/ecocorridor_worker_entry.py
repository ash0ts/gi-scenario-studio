"""PyInstaller entrypoint kept outside the importable scientific package."""

from __future__ import annotations

import json
import math
import os
import platform
import socket
import sys

PACKAGED_RUNTIME_PACKAGES = (
    "geopandas",
    "numpy",
    "openai",
    "openai-agents",
    "pydantic",
    "pyogrio",
    "rasterio",
    "scikit-learn",
    "scipy",
    "shapely",
    "wandb",
    "weave",
)
PACKAGED_RUNTIME_CAPABILITIES = (
    "pydantic_contracts",
    "scipy_sobol",
    "sklearn_extratrees",
    "wandb_offline_table_artifact",
    "deterministic_external_redaction",
    "openai_agents_structured_tool_runtime",
    "weave_openai_agents_processor",
)
FORBIDDEN_PROBE_CREDENTIALS = (
    "ECOCORRIDOR_PROVIDER_API_KEY",
    "OPENAI_API_KEY",
    "WANDB_API_KEY",
    "WANDB_IDENTITY_TOKEN_FILE",
    "WEAVE_API_KEY",
)
REQUIRED_PROBE_STORAGE = (
    "WANDB_ARTIFACT_DIR",
    "WANDB_CACHE_DIR",
    "WANDB_DATA_DIR",
    "WANDB_DIR",
)


def _require_offline_credential_free_probe() -> None:
    if os.environ.get("WANDB_MODE") != "offline":
        raise RuntimeError("The packaged professional-runtime probe requires W&B offline mode.")
    present = sorted(name for name in FORBIDDEN_PROBE_CREDENTIALS if name in os.environ)
    if present:
        raise RuntimeError(
            "The packaged professional-runtime probe refuses cloud or provider credentials: "
            + ", ".join(present)
        )
    missing_storage = sorted(
        name
        for name in REQUIRED_PROBE_STORAGE
        if not os.environ.get(name) or not os.path.isdir(os.environ[name])
    )
    if missing_storage:
        raise RuntimeError(
            "The packaged professional-runtime probe requires isolated W&B storage: "
            + ", ".join(missing_storage)
        )


def _probe_professional_runtime() -> dict[str, bool]:
    """Exercise deterministic professional capabilities without a network client."""

    _require_offline_credential_free_probe()

    import agents
    import numpy
    import weave
    from pydantic import BaseModel
    from scenario_platform.observability import redact_external_identifiers
    from scipy.stats import qmc
    from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
    from weave.integrations.openai_agents.openai_agents import WeaveTracingProcessor
    from weave.utils import sanitize

    import wandb

    if not getattr(agents, "__version__", ""):
        raise RuntimeError("The OpenAI Agents SDK is unavailable.")

    class ProbeContract(BaseModel):
        value: int

    @agents.function_tool
    def resolve_probe_alias(alias: str) -> str:
        """Return one synthetic alias without accessing project state."""

        return alias

    contract = ProbeContract.model_validate({"value": 17})
    if contract.model_dump() != {"value": 17}:
        raise RuntimeError("Pydantic contract validation is unavailable.")
    agent_probe = agents.Agent(
        name="EcoCorridor packaged runtime probe",
        instructions="Use only the synthetic alias tool.",
        model="not-invoked",
        tools=[resolve_probe_alias],
        output_type=ProbeContract,
    )
    if (
        agent_probe.output_type is not ProbeContract
        or len(agent_probe.tools) != 1
        or agent_probe.tools[0].name != "resolve_probe_alias"
        or agent_probe.tools[0].params_json_schema.get("properties", {}).get("alias") is None
    ):
        raise RuntimeError("The OpenAI Agents structured tool runtime is unavailable.")

    sobol = qmc.Sobol(d=2, scramble=True, seed=17).random_base2(m=1)
    if sobol.shape != (2, 2) or not numpy.isfinite(sobol).all():
        raise RuntimeError("SciPy Sobol sampling is unavailable.")
    if not ((sobol >= 0.0).all() and (sobol < 1.0).all()):
        raise RuntimeError("SciPy Sobol sampling returned values outside its unit cube.")

    features = numpy.asarray([[0.0], [1.0], [2.0], [3.0]])
    regressor = ExtraTreesRegressor(n_estimators=4, random_state=17, n_jobs=1).fit(
        features, numpy.asarray([0.0, 1.0, 4.0, 9.0])
    )
    classifier = ExtraTreesClassifier(n_estimators=4, random_state=17, n_jobs=1).fit(
        features, numpy.asarray([0, 0, 1, 1])
    )
    regression = regressor.predict(numpy.asarray([[1.5]]))
    classification = classifier.predict(numpy.asarray([[1.5]]))
    if regression.shape != (1,) or not math.isfinite(float(regression[0])):
        raise RuntimeError("ExtraTrees regression is unavailable.")
    if classification.shape != (1,) or int(classification[0]) not in {0, 1}:
        raise RuntimeError("ExtraTrees classification is unavailable.")

    raw_external_payload = {
        "contact": ("Contact field.team@example.org, 202-555-0182, or record 123-45-6789."),
        "api_key": "must-not-leave-the-worker",
        "note": "Read /Users/example/private/model.txt before upload.",
    }
    original_getaddrinfo = socket.getaddrinfo

    def reject_network(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("packaged redaction may not access the network")

    socket.getaddrinfo = reject_network  # type: ignore[assignment]
    try:
        redacted_external_payload = redact_external_identifiers(raw_external_payload)
    finally:
        socket.getaddrinfo = original_getaddrinfo
    encoded_redacted = json.dumps(redacted_external_payload, sort_keys=True)
    if (
        any(
            raw in encoded_redacted
            for raw in (
                "field.team@example.org",
                "202-555-0182",
                "123-45-6789",
                "must-not-leave-the-worker",
                "/Users/example/private/model.txt",
            )
        )
        or redacted_external_payload != redact_external_identifiers(redacted_external_payload)
        or len(str(redacted_external_payload["contact"])) != len(raw_external_payload["contact"])
    ):
        raise RuntimeError("Deterministic external PII redaction is unavailable.")

    table = wandb.Table(columns=["probe_id", "value"], data=[["offline", 1]])
    artifact = wandb.Artifact("ecocorridor-packaged-runtime-probe", type="runtime-probe")
    artifact.add(table, "runtime_table")
    if (
        table.columns != ["probe_id", "value"]
        or table.data != [["offline", 1]]
        or artifact.name != "ecocorridor-packaged-runtime-probe"
        or artifact.type != "runtime-probe"
        or set(artifact.manifest.entries) != {"runtime_table.table.json"}
    ):
        raise RuntimeError("W&B offline Table or Artifact construction is unavailable.")

    if not callable(weave.init) or not callable(weave.op):
        raise TypeError("The Weave tracing entrypoint is unavailable.")
    processor = WeaveTracingProcessor()
    if not all(
        callable(getattr(processor, member, None))
        for member in (
            "on_trace_start",
            "on_trace_end",
            "on_span_start",
            "on_span_end",
            "force_flush",
        )
    ):
        raise TypeError("The Weave OpenAI Agents tracing processor is unavailable.")
    traced_probe = weave.op(lambda value: value)
    if not callable(traced_probe):
        raise TypeError("The Weave tracing entrypoint could not wrap an operation.")
    redaction_key = "ecocorridor_packaged_runtime_probe_secret"
    for member in ("add_redact_key", "get_redact_keys", "remove_redact_key"):
        if not callable(getattr(sanitize, member, None)):
            raise TypeError("The Weave sensitive-key redaction API is unavailable.")
    sanitize.add_redact_key(redaction_key)
    try:
        if redaction_key not in sanitize.get_redact_keys():
            raise RuntimeError("The Weave sensitive-key redaction API is unavailable.")
    finally:
        sanitize.remove_redact_key(redaction_key)
    if redaction_key in sanitize.get_redact_keys():
        raise RuntimeError("The Weave sensitive-key redaction probe did not clean up.")

    return dict.fromkeys(PACKAGED_RUNTIME_CAPABILITIES, True)


def packaged_runtime_inventory() -> dict[str, object]:
    """Import and exercise every packaged professional runtime dependency."""

    import agents
    import geopandas
    import numpy
    import openai
    import pydantic
    import pyogrio
    import rasterio
    import scipy
    import shapely
    import sklearn
    import weave

    import wandb

    return {
        "schema_version": "1.0",
        "python_version": platform.python_version(),
        "python_packages": {
            "geopandas": geopandas.__version__,
            "numpy": numpy.__version__,
            "openai": openai.__version__,
            "openai-agents": agents.__version__,
            "pydantic": pydantic.__version__,
            "pyogrio": pyogrio.__version__,
            "rasterio": rasterio.__version__,
            "scikit-learn": sklearn.__version__,
            "scipy": scipy.__version__,
            "shapely": shapely.__version__,
            "wandb": wandb.__version__,
            "weave": weave.__version__,
        },
        "capabilities": _probe_professional_runtime(),
        "gdal_version": rasterio.__gdal_version__,
        "pyogrio_gdal_version": pyogrio.__gdal_version_string__,
        "proj_version": rasterio.__proj_version__,
        "geos_version": shapely.geos_version_string,
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["--packaged-runtime-inventory"]:
        print(json.dumps(packaged_runtime_inventory(), sort_keys=True))
    elif sys.argv[1:] == ["--review-brief-agent-subprocess"]:
        from ecocorridor_core.review_brief_agent_process import main as review_brief_main

        raise SystemExit(review_brief_main())
    elif sys.argv[1:] == ["--native-assistant-agent-subprocess"]:
        from ecocorridor_core.native_agents_process import main as native_assistant_main

        raise SystemExit(native_assistant_main())
    else:
        from ecocorridor_core.rpc_worker import main

        main()
