from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "story" / "research-resources.json"
RESEARCH_BASIS = ROOT.parents[1] / "docs" / "research-basis.md"


def _resources() -> dict[str, object]:
    return json.loads(RESOURCES.read_text(encoding="utf-8"))


def test_cedar_trace_ecological_boundary_is_explicit_and_machine_readable() -> None:
    ledger = _resources()
    boundary = ledger["cedar_trace_boundary"]
    exclusions = " ".join(boundary["does_not_measure"]).lower()

    assert ledger["schema_version"] == "1.0"
    assert "synthetic" in boundary["measures"].lower()
    for required in (
        "water quality",
        "groundwater recharge",
        "habitat",
        "heat-island",
        "real flood",
        "embodied carbon",
        "computational energy",
    ):
        assert required in exclusions
    assert "runoff-accounting term" in exclusions
    assert "separate evidence ledgers" in boundary["interpretation"]


def test_environmental_and_lifecycle_sources_preserve_claim_limits() -> None:
    ledger = _resources()
    sources = ledger["sources"]
    indexed = {source["id"]: source for source in sources}

    assert len(indexed) == len(sources)
    for source in sources:
        assert source["supports"]
        assert source["does_not_support"]

    for source_id in (
        "epa-gi-environmental-benefits",
        "epa-gi-groundwater-protection",
    ):
        source = indexed[source_id]
        assert source["kind"] == "official_resource"
        assert source["publisher"] == "U.S. Environmental Protection Agency"
        assert source["url"].startswith("https://www.epa.gov/")

    for source_id, doi in (
        ("flynn-traver-2013", "10.1016/j.ecoleng.2013.01.004"),
        ("vineyard-2015", "10.1111/1752-1688.12320"),
        ("osullivan-2015", "10.1016/j.jenvman.2014.10.025"),
    ):
        source = indexed[source_id]
        assert source["kind"] == "peer_reviewed_primary"
        assert source["doi"] == doi
        assert source["url"] == f"https://doi.org/{doi}"
        assert "Cedar Trace" in source["does_not_support"] or "different site" in source[
            "does_not_support"
        ]


def test_research_basis_names_every_unmeasured_outcome() -> None:
    research_basis = RESEARCH_BASIS.read_text(encoding="utf-8")

    for heading in (
        "**Water quality:**",
        "**Groundwater recharge:**",
        "**Habitat and heat:**",
        "**Real flood risk:**",
        "**Embodied and operational impact:**",
        "**Computational impact:**",
    ):
        assert heading in research_basis
    assert "SWMM `infiltration loss` is a runoff-accounting" in research_basis
    assert "do not turn modeled runoff changes into measured" in research_basis


def test_weave_agent_experiment_sources_preserve_the_observability_boundary() -> None:
    ledger = _resources()
    indexed = {source["id"]: source for source in ledger["sources"]}
    source = indexed["wandb-weave-agent-evaluation"]

    assert source["kind"] == "official_resource"
    assert source["publisher"] == "Weights & Biases"
    assert {
        "https://docs.wandb.ai/weave/guides/tracking/trace-agents",
        "https://docs.wandb.ai/weave/guides/core-types/evaluations",
        "https://docs.wandb.ai/inference/response-settings/structured-output",
    }.issubset(source["urls"])
    assert "does not" not in source["supports"].lower()
    assert "proof" in source["does_not_support"].lower()
    assert "failed" in source["does_not_support"].lower()


def test_evidence_following_study_sources_bound_the_benchmark_claim() -> None:
    ledger = _resources()
    indexed = {source["id"]: source for source in ledger["sources"]}

    checklist = indexed["ribeiro-checklist-2020"]
    assert checklist["doi"] == "10.18653/v1/2020.acl-main.442"
    assert "directional-expectation" in checklist["supports"]
    assert "general model capability" in checklist["does_not_support"]

    nist = indexed["nist-ai-800-3-2026"]
    assert nist["publisher"] == "National Institute of Standards and Technology"
    assert nist["doi"] == "10.6028/NIST.AI.800-3"
    assert "fixed benchmark" in nist["supports"]
    assert "generalized model accuracy" in nist["does_not_support"]
