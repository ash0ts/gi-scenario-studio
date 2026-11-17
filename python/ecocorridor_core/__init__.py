"""Auditable wildlife-connectivity scenario analysis.

The package is intentionally independent from the SWMM domain package.  Public
contracts live in :mod:`ecocorridor_core.contracts`; direct numerical claims are
accepted only from the pinned Circuitscape adapter.
"""

from .contracts import ECOCORRIDOR_SCHEMA_VERSION

__all__ = ["ECOCORRIDOR_SCHEMA_VERSION"]
