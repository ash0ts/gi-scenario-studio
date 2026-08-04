"""PyInstaller entrypoint kept outside the importable scientific package."""

from __future__ import annotations

import json
import platform
import sys


def packaged_runtime_inventory() -> dict[str, object]:
    """Import every packaged planning runtime and report path-free versions."""

    import geopandas
    import numpy
    import pyogrio
    import rasterio
    import shapely

    return {
        "schema_version": "1.0",
        "python_version": platform.python_version(),
        "python_packages": {
            "geopandas": geopandas.__version__,
            "numpy": numpy.__version__,
            "pyogrio": pyogrio.__version__,
            "rasterio": rasterio.__version__,
            "shapely": shapely.__version__,
        },
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
