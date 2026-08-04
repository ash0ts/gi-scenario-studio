"""PyInstaller entrypoint kept outside the importable scientific package."""

from __future__ import annotations

import json
import math
import os
import platform
import sys

PACKAGED_RUNTIME_PACKAGES = (
    "geopandas",
    "numpy",
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
    "weave_entrypoint_redaction",
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

    import numpy
    import wandb
    import weave
    from pydantic import BaseModel
    from scipy.stats import qmc
    from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
    from weave.utils import sanitize

    class ProbeContract(BaseModel):
        value: int

    contract = ProbeContract.model_validate({"value": 17})
    if contract.model_dump() != {"value": 17}:
        raise RuntimeError("Pydantic contract validation is unavailable.")

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

    import geopandas
    import numpy
    import pydantic
    import pyogrio
    import rasterio
    import scipy
    import shapely
    import sklearn
    import wandb
    import weave

    return {
        "schema_version": "1.0",
        "python_version": platform.python_version(),
        "python_packages": {
            "geopandas": geopandas.__version__,
            "numpy": numpy.__version__,
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
    else:
        from ecocorridor_core.rpc_worker import main

        main()
