"""Scientific and domain core for GI Scenario Studio.

The package is deliberately usable without a desktop shell.  All public payloads
live in :mod:`gi_scenario_core.contracts`, and the JSONL worker exposes the same
services used by the Tauri application.
"""

from .contracts import (
    CONCEPTUAL_NOTICE,
    AnalysisState,
    CandidateSlot,
    ConceptModelRecipe,
    CostBasis,
    ExactRunResult,
    ModelReadiness,
    ProjectBundle,
    ReadinessLevel,
    SimulationBudget,
    SolverJobState,
    StormCase,
)

__all__ = [
    "CONCEPTUAL_NOTICE",
    "AnalysisState",
    "CandidateSlot",
    "ConceptModelRecipe",
    "CostBasis",
    "ExactRunResult",
    "ModelReadiness",
    "ProjectBundle",
    "ReadinessLevel",
    "SimulationBudget",
    "SolverJobState",
    "StormCase",
]

__version__ = "0.1.0"
