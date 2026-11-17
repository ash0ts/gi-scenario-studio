"""Deterministic, read-only evidence projections for the bounded Evidence Guide.

The service deliberately exposes no generic SQL, filesystem, model mutation, or
solver execution.  Every tool response is rebuilt from the authoritative local
repository or the hash-pinned Cedar Trace fixture before it is returned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import (
    AssistantEvidenceAnchor,
    AssistantEvidencePacket,
    AssistantEvidenceReference,
    EvidenceAuthority,
    EvidenceClaim,
    ProjectKind,
)
from .orchestrator import AnalysisOrchestrator, extract_runoff_quantity_ledger
from .repository import ProjectRepository

PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "decision.get_rule_matrix",
    "results.compare_exact",
    "results.get_runoff_ledger",
    "evidence.resolve",
    "model.get_fitness",
    "project.list_limitations",
)

MAX_PACKET_REFERENCES = 768
MAX_PACKET_CLAIMS = 1_536
MAX_PACKET_BYTES = 768_000

_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "decision.get_rule_matrix": {
        "description": "Read the stored decision rules and their pass/fail results.",
        "properties": {"alternative_id": {"type": "string"}},
    },
    "results.compare_exact": {
        "description": "Compare one complete exact alternative ensemble with its baseline.",
        "properties": {"alternative_id": {"type": "string"}},
    },
    "results.get_runoff_ledger": {
        "description": "Read SWMM runoff-accounting quantities from retained report evidence.",
        "properties": {
            "alternative_id": {"type": "string"},
            "storm_id": {"type": "string"},
            "run_id": {"type": "string"},
        },
    },
    "evidence.resolve": {
        "description": "Resolve an evidence or claim ID from the current packet.",
        "properties": {"evidence_id": {"type": "string"}},
    },
    "model.get_fitness": {
        "description": "Read the engineer-controlled Model Fitness Passport.",
        "properties": {},
    },
    "project.list_limitations": {
        "description": "List project, model-fitness, and fixture limitations.",
        "properties": {},
    },
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_sha256_bytes(_canonical_bytes(value))[:24]}"


def _packet_digest(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("packet_id", None)
    normalized.pop("packet_sha256", None)
    return _sha256_bytes(_canonical_bytes(normalized))


def _safe_fixture_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("guided evidence path must be a safe relative path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("guided evidence path cannot traverse a symbolic link")
    resolved = current.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("guided evidence path escapes the project directory")
    return resolved


class EvidenceQueryService:
    """Build immutable evidence packets and execute six allowlisted read tools."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        self._handlers: dict[
            str, Callable[[AssistantEvidencePacket, dict[str, Any]], dict[str, Any]]
        ] = {
            "decision.get_rule_matrix": self._decision_rule_matrix,
            "results.compare_exact": self._compare_exact,
            "results.get_runoff_ledger": self._runoff_ledger,
            "evidence.resolve": self._resolve_evidence,
            "model.get_fitness": self._model_fitness,
            "project.list_limitations": self._list_limitations,
        }

    def build_packet(
        self,
        project_id: str,
        analysis_id: str | None,
        model_revision_id: str | None,
        anchor: AssistantEvidenceAnchor | dict[str, Any] | str,
    ) -> AssistantEvidencePacket:
        project = self.repository.get_project(project_id)
        normalized_anchor = self._normalize_anchor(project_id, anchor)
        analysis: dict[str, Any] | None = None
        revision: dict[str, Any] | None = None
        if analysis_id is not None:
            analysis = self.repository.get_analysis(analysis_id)
            if analysis["project_id"] != project_id:
                raise ValueError("assistant evidence analysis belongs to another project")
            analysis_revision = analysis["payload"].get("model_revision_id")
            if model_revision_id is None and isinstance(analysis_revision, str):
                model_revision_id = analysis_revision
            elif (
                model_revision_id is not None
                and analysis_revision is not None
                and analysis_revision != model_revision_id
            ):
                raise ValueError("assistant evidence revision does not match its analysis")
        if model_revision_id is not None:
            try:
                revision = self.repository.get_model_revision(model_revision_id)
            except KeyError:
                if project["project_kind"] != ProjectKind.DEMO.value:
                    raise
            else:
                if revision["project_id"] != project_id:
                    raise ValueError("assistant evidence revision belongs to another project")

        references: list[AssistantEvidenceReference] = []
        claims: list[EvidenceClaim] = []
        source_hashes = {
            "project_record": _sha256_bytes(_canonical_bytes(project)),
        }
        if revision is not None:
            source_hashes["model_revision"] = _sha256_bytes(_canonical_bytes(revision))
        claim_time = project["updated_at"]
        project_evidence_id = f"project:{project_id}"
        project_label = (
            project["title"]
            if project["project_kind"] == ProjectKind.DEMO.value
            else "Current engineering project"
        )
        references.append(
            AssistantEvidenceReference(
                evidence_id=project_evidence_id,
                kind="project",
                label=project_label,
                source_id=project_id,
            )
        )
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="provenance",
                statement=(
                    f"{project_label} is a {project['project_kind']} project. "
                    "The local repository remains authoritative."
                ),
                evidence_ids=[project_evidence_id],
                authority=EvidenceAuthority.PROJECT_RECORD,
                source_ids=[project_id],
                created_at=claim_time,
            )
        )
        passport = self.repository.get_model_fitness_passport(project_id, model_revision_id)
        source_hashes["model_fitness"] = _sha256_bytes(
            _canonical_bytes(
                passport
                if passport is not None
                else {
                    "project_id": project_id,
                    "model_revision_id": model_revision_id,
                    "disposition": "not_assessed",
                }
            )
        )
        fitness_id = (
            f"model-fitness:{passport['passport_id']}"
            if passport
            else f"model-fitness:{project_id}:not-assessed"
        )
        references.append(
            AssistantEvidenceReference(
                evidence_id=fitness_id,
                kind="model_fitness",
                label="Model Fitness Passport" if passport else "Model fitness not assessed",
                source_id=passport["passport_id"] if passport else project_id,
            )
        )
        disposition = passport["disposition"] if passport else "not_assessed"
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="fitness",
                statement=f"Model fitness for the stated use is {disposition}.",
                evidence_ids=[fitness_id],
                authority=EvidenceAuthority.MODEL_FITNESS,
                source_ids=[passport["passport_id"] if passport else project_id],
                created_at=claim_time,
            )
        )
        if passport:
            for criterion in passport.get("criteria", []):
                criterion_id = str(criterion["criterion_id"])
                evidence_id = f"model-fitness-criterion:{passport['passport_id']}:{criterion_id}"
                references.append(
                    AssistantEvidenceReference(
                        evidence_id=evidence_id,
                        kind="model_fitness",
                        label=str(criterion["label"]),
                        source_id=criterion_id,
                    )
                )
                finding = str(criterion.get("finding") or "No finding recorded.")
                claims.append(
                    self._claim(
                        project_id=project_id,
                        claim_type="fitness",
                        statement=(
                            f"Model fitness criterion {criterion['label']} is "
                            f"{criterion['status']}: {finding}"
                        ),
                        evidence_ids=[evidence_id, fitness_id],
                        authority=EvidenceAuthority.MODEL_FITNESS,
                        source_ids=[str(passport["passport_id"]), criterion_id],
                        created_at=claim_time,
                    )
                )

        fixture: dict[str, Any] | None = None
        if project["project_kind"] == ProjectKind.DEMO.value:
            fixture, fixture_hashes = self._load_guided_fixture(project)
            source_hashes.update(fixture_hashes)
            if normalized_anchor.kind == "alternative":
                self._append_guided_alternative_evidence(
                    project_id,
                    fixture,
                    normalized_anchor.identifier,
                    references,
                    claims,
                    claim_time,
                )
        elif normalized_anchor.kind == "alternative":
            if analysis_id is None:
                raise ValueError(
                    "a practitioner alternative requires its authoritative analysis scope"
                )
            board = AnalysisOrchestrator(self.repository).decision_board(analysis_id)
            source_hashes["decision_board"] = _sha256_bytes(_canonical_bytes(board))
            brief = self.repository.get_decision_brief(project_id, analysis_id)
            if brief is not None:
                source_hashes["decision_brief"] = _sha256_bytes(_canonical_bytes(brief))
            self._append_repository_alternative_evidence(
                project_id,
                normalized_anchor.identifier,
                board,
                brief,
                references,
                claims,
                claim_time,
            )
        elif normalized_anchor.kind == "exact_run":
            self._append_repository_run_evidence(
                project_id,
                normalized_anchor.identifier,
                references,
                claims,
                claim_time,
            )

        if analysis is not None:
            source_hashes["analysis_record"] = _sha256_bytes(_canonical_bytes(analysis))
        limitations = self._project_limitations(project, passport, fixture)
        for index, limitation in enumerate(limitations):
            evidence_id = f"limitation:{project_id}:{index + 1}"
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="limitation",
                    label="Project limitation",
                    source_id=project_id,
                )
            )
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="limitation",
                    statement=limitation,
                    evidence_ids=[evidence_id],
                    authority=EvidenceAuthority.PROJECT_RECORD,
                    source_ids=[project_id],
                    created_at=claim_time,
                )
            )

        payload = {
            "packet_id": "pending",
            "project_id": project_id,
            "analysis_id": analysis_id,
            "model_revision_id": model_revision_id,
            "anchor": normalized_anchor.model_dump(mode="json"),
            "project_kind": project["project_kind"],
            "evidence_references": [item.model_dump(mode="json") for item in references],
            "claims": [item.model_dump(mode="json") for item in claims],
            "limitations": limitations,
            "source_hashes": dict(sorted(source_hashes.items())),
            "capability_names": list(PUBLIC_TOOL_NAMES),
            "packet_sha256": "0" * 64,
            "local_authority": True,
        }
        self._validate_packet_bounds(payload)
        digest = _packet_digest(payload)
        payload.update({"packet_id": f"packet_{digest[:24]}", "packet_sha256": digest})
        return AssistantEvidencePacket.model_validate(payload)

    def capabilities(
        self, packet: AssistantEvidencePacket | dict[str, Any]
    ) -> list[dict[str, Any]]:
        validated = self._validate_packet(packet)
        is_guided_alternative = (
            validated.project_kind == ProjectKind.DEMO.value
            and validated.anchor.kind == "alternative"
        )
        has_analysis = validated.analysis_id is not None
        result: list[dict[str, Any]] = []
        for name in PUBLIC_TOOL_NAMES:
            available = True
            reason: str | None = None
            if name in {"decision.get_rule_matrix", "results.compare_exact"} and not (
                validated.anchor.kind == "alternative" and (is_guided_alternative or has_analysis)
            ):
                available = False
                reason = "Select an alternative with a complete exact ensemble first."
            if name == "results.get_runoff_ledger" and not (
                is_guided_alternative or validated.anchor.kind == "exact_run"
            ):
                available = False
                reason = "Select an alternative or retained exact run report first."
            result.append(
                {
                    "name": name,
                    "description": _TOOL_SPECS[name]["description"],
                    "read_only": True,
                    "available": available,
                    "unavailable_reason": reason,
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": _TOOL_SPECS[name]["properties"],
                    },
                }
            )
        return result

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        packet: AssistantEvidencePacket | dict[str, Any],
    ) -> dict[str, Any]:
        validated = self._validate_packet(packet)
        if tool_name not in self._handlers:
            raise ValueError(f"assistant evidence tool {tool_name!r} is not allowlisted")
        if not isinstance(arguments, dict):
            raise TypeError("assistant evidence tool arguments must be an object")
        allowed = set(_TOOL_SPECS[tool_name]["properties"])
        extras = set(arguments).difference(allowed)
        if extras:
            raise ValueError(
                f"assistant evidence tool {tool_name!r} received unsupported arguments: "
                + ", ".join(sorted(extras))
            )
        capability = next(
            item for item in self.capabilities(validated) if item["name"] == tool_name
        )
        if not capability["available"]:
            raise ValueError(str(capability["unavailable_reason"]))
        result = self._handlers[tool_name](validated, arguments)
        claim_receipts = self._claim_receipts_for_tool(
            tool_name,
            validated,
            result,
        )
        return {
            "tool": tool_name,
            "packet_id": validated.packet_id,
            "packet_sha256": validated.packet_sha256,
            "project_id": validated.project_id,
            "local_authority": True,
            "claim_receipts": claim_receipts,
            "result": result,
        }

    @staticmethod
    def _claim_receipts_for_tool(
        tool_name: str,
        packet: AssistantEvidencePacket,
        result: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Bind a tool response to the exact packet claims it authorizes.

        The explanation renderer consumes these receipts, never a broad claim
        type inferred from the visible tool name.  ``evidence.resolve`` is
        narrower still: it authorizes only the claims the resolver actually
        returned.
        """

        resolved_ids: set[str]
        if tool_name == "evidence.resolve":
            raw_claims = result.get("claims")
            if not isinstance(raw_claims, list):
                raise RuntimeError("evidence resolver returned malformed claims")
            resolved_ids = {
                str(item.get("claim_id"))
                for item in raw_claims
                if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
            }
        else:
            resolved_ids = EvidenceQueryService._claim_ids_bound_to_result(
                tool_name,
                packet,
                result,
            )

        receipts: list[dict[str, str]] = []
        for claim in packet.claims:
            if claim.claim_id not in resolved_ids:
                continue
            projection = claim.model_dump(mode="json")
            receipts.append(
                {
                    "claim_id": claim.claim_id,
                    "claim_sha256": _sha256_bytes(_canonical_bytes(projection)),
                }
            )
        return sorted(receipts, key=lambda item: item["claim_id"])

    @staticmethod
    def _claim_ids_bound_to_result(
        tool_name: str,
        packet: AssistantEvidencePacket,
        result: dict[str, Any],
    ) -> set[str]:
        """Independently bind a public tool projection to canonical packet claims.

        A tool name alone never authorizes a whole claim type.  Each projection
        must contain the subject- and source-specific material required below;
        malformed or empty projections fail before a receipt can be minted.
        """

        if not isinstance(result, dict) or not result:
            raise RuntimeError("evidence tool returned an empty or malformed projection")
        if tool_name == "project.list_limitations":
            limitations = result.get("limitations")
            if limitations != packet.limitations or result.get("count") != len(packet.limitations):
                raise RuntimeError("limitation projection does not match the evidence packet")
            return {claim.claim_id for claim in packet.claims if claim.claim_type == "limitation"}
        if tool_name == "model.get_fitness":
            if result.get("engineer_controlled") is not True or "disposition" not in result:
                raise RuntimeError("model-fitness projection is malformed")
            disposition = str(result["disposition"])
            matching = {
                claim.claim_id
                for claim in packet.claims
                if claim.claim_type == "fitness"
                and (
                    f" is {disposition}." in claim.statement
                    or result.get("passport") is not None
                    or result.get("criteria") is not None
                )
            }
            if not matching:
                raise RuntimeError("model-fitness projection has no bound packet claim")
            return matching
        if tool_name == "decision.get_rule_matrix":
            alternative_id = str(
                result.get("alternative_id")
                or (result.get("alternative") or {}).get("design_id")
                or ""
            )
            if alternative_id and alternative_id != packet.anchor.identifier:
                raise RuntimeError("decision projection belongs to another subject")
            checks = result.get("checks")
            if checks is None and isinstance(result.get("alternative"), dict):
                checks = result["alternative"].get("criteria")
            if not isinstance(result.get("rules"), list) or not isinstance(checks, list):
                raise RuntimeError("decision projection is malformed")
            matching = {
                claim.claim_id
                for claim in packet.claims
                if claim.claim_type in {"decision_rule", "cost", "qa"}
                and (
                    packet.anchor.identifier in claim.statement
                    or packet.anchor.identifier in claim.source_ids
                    or claim.claim_type == "qa"
                )
            }
            if checks and not matching:
                raise RuntimeError("decision projection has no bound packet claim")
            EvidenceQueryService._validate_decision_claim_values(packet, checks)
            return matching
        if tool_name in {"results.compare_exact", "results.get_runoff_ledger"}:
            if tool_name == "results.compare_exact" and result.get("exact") is not True:
                raise RuntimeError("exact comparison projection is not marked exact")
            alternative_id = str(
                result.get("alternative_id")
                or (result.get("alternative") or {}).get("design_id")
                or ""
            )
            if (
                alternative_id
                and packet.anchor.kind == "alternative"
                and (alternative_id != packet.anchor.identifier)
            ):
                raise RuntimeError("hydraulic projection belongs to another subject")
            run_ids = EvidenceQueryService._result_run_ids(result)
            if not run_ids:
                raise RuntimeError("hydraulic projection contains no exact run identity")
            matching: set[str] = set()
            for claim in packet.claims:
                if claim.claim_type not in {"exact_metric", "derived_metric", "qa"}:
                    continue
                sources = set(claim.run_ids or ([] if claim.run_id is None else [claim.run_id]))
                if sources and sources.issubset(run_ids):
                    matching.add(claim.claim_id)
            if tool_name == "results.compare_exact" and isinstance(result.get("screening"), dict):
                matching.update(
                    claim.claim_id
                    for claim in packet.claims
                    if claim.claim_type in {"decision_rule", "cost"}
                    and packet.anchor.identifier in claim.statement
                )
                EvidenceQueryService._validate_decision_claim_values(
                    packet,
                    result["screening"].get("checks"),
                )
            if tool_name == "results.compare_exact":
                EvidenceQueryService._validate_exact_comparison_values(
                    packet,
                    result,
                    matching,
                )
            if not matching:
                raise RuntimeError("hydraulic projection has no bound packet claim")
            return matching
        raise RuntimeError("evidence tool has no claim-binding policy")

    @staticmethod
    def _result_run_ids(value: Any, *, key: str = "") -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for child_key, child in value.items():
                if isinstance(child, str) and (
                    child_key.endswith("run_id") or child_key == "run_id"
                ):
                    found.add(child)
                else:
                    found.update(EvidenceQueryService._result_run_ids(child, key=str(child_key)))
        elif isinstance(value, list):
            for child in value:
                found.update(EvidenceQueryService._result_run_ids(child, key=key))
        elif isinstance(value, str) and key.endswith("run_ids"):
            found.add(value)
        return found

    @staticmethod
    def _validate_decision_claim_values(
        packet: AssistantEvidencePacket,
        checks: Any,
    ) -> None:
        if not isinstance(checks, list):
            return
        by_rule = {
            str(item.get("rule_id")): item
            for item in checks
            if isinstance(item, dict) and item.get("rule_id") is not None
        }
        for claim in packet.claims:
            if claim.claim_type != "decision_rule":
                continue
            rule = next((by_rule[item] for item in claim.source_ids if item in by_rule), None)
            if rule is None:
                continue
            expected_pass = " passes " in f" {claim.statement.casefold()} "
            expected_fail = " fails " in f" {claim.statement.casefold()} "
            if (
                not isinstance(rule.get("passes"), bool)
                or (expected_pass and rule["passes"] is not True)
                or (expected_fail and rule["passes"] is not False)
            ):
                raise RuntimeError("decision projection conflicts with its packet claim")

    @staticmethod
    def _validate_exact_comparison_values(
        packet: AssistantEvidencePacket,
        result: dict[str, Any],
        claim_ids: set[str],
    ) -> None:
        storms = result.get("storms")
        if not isinstance(storms, list):
            return
        by_run: dict[str, tuple[dict[str, Any], str]] = {}
        for item in storms:
            if not isinstance(item, dict):
                continue
            baseline_id = item.get("baseline_run_id")
            alternative_id = item.get("alternative_run_id")
            if isinstance(baseline_id, str):
                by_run[baseline_id] = (item, "baseline")
            if isinstance(alternative_id, str):
                by_run[alternative_id] = (item, "alternative")
        claims = {claim.claim_id: claim for claim in packet.claims}
        for claim_id in claim_ids:
            claim = claims[claim_id]
            if claim.value is None or isinstance(claim.value, bool):
                continue
            if claim.claim_type == "exact_metric" and claim.run_id in by_run:
                storm, side = by_run[str(claim.run_id)]
                key = {
                    ("outfall_peak_cfs", "baseline"): "baseline_outfall_peak_cfs",
                    ("outfall_peak_cfs", "alternative"): "alternative_outfall_peak_cfs",
                    (
                        "outfall_volume_million_gallons",
                        "baseline",
                    ): "baseline_outfall_volume_million_gallons",
                    (
                        "outfall_volume_million_gallons",
                        "alternative",
                    ): "alternative_outfall_volume_million_gallons",
                }.get((str(claim.metric_id), side))
                if key and not EvidenceQueryService._same_number(storm.get(key), claim.value):
                    raise RuntimeError("exact comparison conflicts with its packet claim")
            elif claim.metric_id == "four_storm_outfall_volume_reduction_percent":
                if not EvidenceQueryService._same_number(
                    result.get("outfall_volume_reduction_percent"),
                    claim.value,
                ):
                    raise RuntimeError("exact comparison conflicts with its packet claim")
            elif claim.metric_id == "outfall_peak_delta_cfs" and claim.run_ids:
                alternative = next(
                    (
                        by_run[run_id][0]
                        for run_id in claim.run_ids
                        if run_id in by_run and by_run[run_id][1] == "alternative"
                    ),
                    None,
                )
                if alternative is not None and not EvidenceQueryService._same_number(
                    alternative.get("outfall_peak_delta_cfs"),
                    claim.value,
                ):
                    raise RuntimeError("exact comparison conflicts with its packet claim")

    @staticmethod
    def _same_number(left: Any, right: Any) -> bool:
        if not isinstance(left, (int, float)) or isinstance(left, bool):
            return False
        if not isinstance(right, (int, float)) or isinstance(right, bool):
            return False
        return abs(float(left) - float(right)) <= 1e-12 * max(1.0, abs(float(right)))

    def _validate_packet(
        self, packet: AssistantEvidencePacket | dict[str, Any]
    ) -> AssistantEvidencePacket:
        validated = AssistantEvidencePacket.model_validate(packet)
        payload = validated.model_dump(mode="json")
        self._validate_packet_bounds(payload)
        digest = _packet_digest(payload)
        if validated.packet_sha256 != digest or validated.packet_id != f"packet_{digest[:24]}":
            raise ValueError("assistant evidence packet failed its deterministic digest check")
        project = self.repository.get_project(validated.project_id)
        current_hash = _sha256_bytes(_canonical_bytes(project))
        if validated.source_hashes.get("project_record") != current_hash:
            raise ValueError("assistant evidence packet is stale for the current project record")
        current_passport = self.repository.get_model_fitness_passport(
            validated.project_id, validated.model_revision_id
        )
        passport_hash = _sha256_bytes(
            _canonical_bytes(
                current_passport
                if current_passport is not None
                else {
                    "project_id": validated.project_id,
                    "model_revision_id": validated.model_revision_id,
                    "disposition": "not_assessed",
                }
            )
        )
        if validated.source_hashes.get("model_fitness") != passport_hash:
            raise ValueError("assistant evidence packet is stale for model fitness evidence")
        if validated.analysis_id is not None:
            analysis = self.repository.get_analysis(validated.analysis_id)
            if analysis["project_id"] != validated.project_id or validated.source_hashes.get(
                "analysis_record"
            ) != _sha256_bytes(_canonical_bytes(analysis)):
                raise ValueError("assistant evidence packet is stale for the selected analysis")
            if "decision_board" in validated.source_hashes:
                board = AnalysisOrchestrator(self.repository).decision_board(validated.analysis_id)
                if validated.source_hashes["decision_board"] != _sha256_bytes(
                    _canonical_bytes(board)
                ):
                    raise ValueError(
                        "assistant evidence packet is stale for the exact decision board"
                    )
            if "decision_brief" in validated.source_hashes:
                brief = self.repository.get_decision_brief(
                    validated.project_id, validated.analysis_id
                )
                if brief is None or validated.source_hashes["decision_brief"] != _sha256_bytes(
                    _canonical_bytes(brief)
                ):
                    raise ValueError("assistant evidence packet is stale for the Decision Brief")
        if validated.model_revision_id is not None and "model_revision" in validated.source_hashes:
            revision = self.repository.get_model_revision(validated.model_revision_id)
            if revision["project_id"] != validated.project_id or validated.source_hashes.get(
                "model_revision"
            ) != _sha256_bytes(_canonical_bytes(revision)):
                raise ValueError("assistant evidence packet is stale for the model revision")
        return validated

    @staticmethod
    def _validate_packet_bounds(payload: dict[str, Any]) -> None:
        references = payload.get("evidence_references", [])
        claims = payload.get("claims", [])
        if len(references) > MAX_PACKET_REFERENCES or len(claims) > MAX_PACKET_CLAIMS:
            raise ValueError("the selected evidence exceeds the bounded Evidence Guide context")
        if len(_canonical_bytes(payload)) > MAX_PACKET_BYTES:
            raise ValueError(
                "the selected evidence packet is too large for the bounded Evidence Guide"
            )

    @staticmethod
    def _normalize_anchor(
        project_id: str,
        anchor: AssistantEvidenceAnchor | dict[str, Any] | str,
    ) -> AssistantEvidenceAnchor:
        if isinstance(anchor, str):
            if anchor == project_id:
                return AssistantEvidenceAnchor(kind="project", identifier=anchor)
            if anchor.startswith("ALT-"):
                return AssistantEvidenceAnchor(kind="alternative", identifier=anchor)
            if anchor.startswith("run_"):
                return AssistantEvidenceAnchor(kind="exact_run", identifier=anchor)
            raise ValueError(
                "string evidence anchors must identify the project, alternative, or run"
            )
        return AssistantEvidenceAnchor.model_validate(anchor)

    def _load_guided_fixture(
        self, project: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        fixture_path = _safe_fixture_path(self.repository.root, "guided_fixture/ui-projection.json")
        manifest_path = _safe_fixture_path(self.repository.root, "guided_fixture/manifest.json")
        if not fixture_path.is_file() or not manifest_path.is_file():
            raise ValueError("guided evidence is not installed in this project")
        fixture_bytes = fixture_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        fixture_sha = _sha256_bytes(fixture_bytes)
        manifest_sha = _sha256_bytes(manifest_bytes)
        if fixture_sha != project["state"].get("guided_fixture_sha256"):
            raise ValueError("guided fixture bytes do not match the project record")
        fixture = json.loads(fixture_bytes)
        if fixture.get("source_manifest_sha256") != manifest_sha:
            raise ValueError("guided fixture and exact-run manifest hashes do not match")
        stored_manifest = project["state"].get("guided_manifest_sha256")
        if stored_manifest is not None and stored_manifest != manifest_sha:
            raise ValueError("guided manifest bytes do not match the project record")
        if fixture.get("fixture_id") != "cedar_trace_exact_v2":
            raise ValueError("unsupported guided fixture identity")
        return fixture, {
            "guided_fixture": fixture_sha,
            "guided_manifest": manifest_sha,
            "guided_source_model": str(fixture["source_model_sha256"]),
        }

    def _append_guided_alternative_evidence(
        self,
        project_id: str,
        fixture: dict[str, Any],
        alternative_id: str,
        references: list[AssistantEvidenceReference],
        claims: list[EvidenceClaim],
        created_at: str,
    ) -> None:
        alternative = self._guided_alternative(fixture, alternative_id)
        alt_evidence_id = f"alternative:{alternative_id}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=alt_evidence_id,
                kind="alternative",
                label=f"{alternative_id} · {alternative['name']}",
                source_id=alternative["design_id"],
            )
        )
        baseline_by_storm = {run["storm_id"]: run for run in fixture["baseline"]["runs"]}
        alternative_run_ids: list[str] = []
        baseline_run_ids: list[str] = []
        for run in alternative["storm_runs"]:
            baseline = baseline_by_storm[run["storm_id"]]
            alternative_run_ids.append(run["run_id"])
            baseline_run_ids.append(baseline["run_id"])
            for source, label in ((baseline, "Baseline"), (run, alternative_id)):
                run_id = source["run_id"]
                run_evidence_id = f"exact-run:{run_id}"
                references.append(
                    AssistantEvidenceReference(
                        evidence_id=run_evidence_id,
                        kind="exact_run",
                        label=f"{label} · {run['storm_id']}",
                        source_id=run_id,
                    )
                )
                metrics = (
                    source["metrics"]
                    if source is baseline
                    else {
                        "outfall_peak_cfs": source["outfall_peak_cfs"],
                        "outfall_volume_million_gallons": source["outfall_volume_million_gallons"],
                    }
                )
                for metric_id, units in (
                    ("outfall_peak_cfs", "cfs"),
                    ("outfall_volume_million_gallons", "million gallons"),
                ):
                    metric_evidence_id = f"exact-metric:{run_id}:{metric_id}:OUT1"
                    references.append(
                        AssistantEvidenceReference(
                            evidence_id=metric_evidence_id,
                            kind="exact_metric",
                            label=f"{label} {metric_id} · {run['storm_id']}",
                            source_id=run_id,
                        )
                    )
                    claims.append(
                        self._claim(
                            project_id=project_id,
                            claim_type="exact_metric",
                            statement=(
                                f"{label} {metric_id} for {run['storm_id']} is "
                                f"{float(metrics[metric_id]):.12g} {units}."
                            ),
                            evidence_ids=[metric_evidence_id],
                            authority=EvidenceAuthority.EXACT_SWMM,
                            source_ids=[run_id],
                            value=float(metrics[metric_id]),
                            units=units,
                            run_id=run_id,
                            run_ids=[run_id],
                            metric_id=metric_id,
                            object_id="OUT1",
                            exact=True,
                            created_at=created_at,
                        )
                    )

        aggregate_sources = [*baseline_run_ids, *alternative_run_ids]
        aggregate_id = f"derived:{alternative_id}:four-storm-volume-reduction"
        references.append(
            AssistantEvidenceReference(
                evidence_id=aggregate_id,
                kind="derived_metric",
                label=f"{alternative_id} four-storm volume reduction",
                source_id=alternative["design_id"],
            )
        )
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="derived_metric",
                statement=(
                    f"{alternative_id} reduces the summed four-storm OUT1 event volume by "
                    f"{alternative['outfall_volume_reduction_percent']:.12g} percent."
                ),
                evidence_ids=[aggregate_id],
                authority=EvidenceAuthority.DERIVED_EXACT,
                source_ids=aggregate_sources,
                value=float(alternative["outfall_volume_reduction_percent"]),
                units="percent",
                run_ids=aggregate_sources,
                metric_id="four_storm_outfall_volume_reduction_percent",
                object_id="OUT1",
                derivation=(
                    "100 * (sum(baseline OUT1 event volume) - sum(alternative OUT1 event volume)) "
                    "/ sum(baseline OUT1 event volume)"
                ),
                exact=True,
                created_at=created_at,
            )
        )

        storm_labels = {
            "synthetic_balanced_1in": "balanced synthetic event",
            "synthetic_front_loaded_2in": "front-loaded synthetic event",
            "synthetic_long_2_5in": "long synthetic event",
            "synthetic_stress_4in": "stress synthetic event",
        }
        for run in alternative["storm_runs"]:
            delta = float(run["outfall_peak_delta_cfs"])
            if delta <= 0:
                continue
            storm_id = str(run["storm_id"])
            baseline = baseline_by_storm[storm_id]
            evidence_id = f"derived:{alternative_id}:adverse-peak:{storm_id}"
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="derived_metric",
                    label=(
                        f"{alternative_id} adverse OUT1 peak · "
                        f"{storm_labels.get(storm_id, storm_id)}"
                    ),
                    source_id=alternative["design_id"],
                )
            )
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="derived_metric",
                    statement=(
                        f"The recorded adverse OUT1 peak for {alternative_id} in the "
                        f"{storm_labels.get(storm_id, storm_id)} ({storm_id}) changes from "
                        f"{float(run['baseline_outfall_peak_cfs']):.12g} to "
                        f"{float(run['outfall_peak_cfs']):.12g} cfs, an increase of "
                        f"{delta:.12g} cfs."
                    ),
                    evidence_ids=[evidence_id],
                    authority=EvidenceAuthority.DERIVED_EXACT,
                    source_ids=[baseline["run_id"], run["run_id"]],
                    value=delta,
                    units="cfs",
                    run_ids=[baseline["run_id"], run["run_id"]],
                    metric_id="outfall_peak_delta_cfs",
                    object_id="OUT1",
                    derivation=(
                        "alternative OUT1 peak flow minus matching baseline OUT1 peak flow"
                    ),
                    exact=True,
                    created_at=created_at,
                )
            )

        cost_basis = fixture["cost_basis"]
        cost_evidence_id = f"cost-basis:{cost_basis['basis_id']}:{alternative_id}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=cost_evidence_id,
                kind="cost_basis",
                label=f"{alternative_id} synthetic present-value cost",
                source_id=cost_basis["basis_id"],
            )
        )
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="cost",
                statement=(
                    f"{alternative_id} has a synthetic present-value screening cost of "
                    f"{alternative['present_value_cost_thousand_usd']:.12g} thousand USD."
                ),
                evidence_ids=[cost_evidence_id],
                authority=EvidenceAuthority.SOURCED_INPUT,
                source_ids=[cost_basis["basis_id"]],
                value=float(alternative["present_value_cost_thousand_usd"]),
                units="thousand USD",
                exact=False,
                created_at=created_at,
            )
        )

        for check in alternative["screening"]["checks"]:
            rule_id = str(check["rule_id"])
            rule_evidence_id = f"decision-rule:{rule_id}"
            if all(item.evidence_id != rule_evidence_id for item in references):
                references.append(
                    AssistantEvidenceReference(
                        evidence_id=rule_evidence_id,
                        kind="decision_rule",
                        label=str(check["label"]),
                        source_id=rule_id,
                    )
                )
            threshold = check["threshold"]
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="decision_rule",
                    statement=(
                        f"{alternative_id} {'passes' if check['passes'] else 'fails'} "
                        f"the {check['label']} rule."
                    ),
                    evidence_ids=[rule_evidence_id, alt_evidence_id],
                    authority=EvidenceAuthority.DECISION_RULE,
                    source_ids=[rule_id, alternative["design_id"]],
                    value=float(threshold) if isinstance(threshold, (int, float)) else None,
                    units=str(check["units"]) if isinstance(threshold, (int, float)) else None,
                    exact=False,
                    created_at=created_at,
                )
            )

        worst = alternative["screening"]["worst_peak_change"]
        baseline_run = baseline_by_storm[worst["storm_id"]]
        alternative_run = next(
            run for run in alternative["storm_runs"] if run["storm_id"] == worst["storm_id"]
        )
        adverse_id = f"derived:{alternative_id}:peak-delta:{worst['storm_id']}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=adverse_id,
                kind="derived_metric",
                label=f"{alternative_id} worst selected-storm OUT1 peak change",
                source_id=alternative["design_id"],
            )
        )
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="derived_metric",
                statement=(
                    f"The worst selected-storm OUT1 peak change for {alternative_id} is "
                    f"{float(worst['delta_cfs']):.12g} cfs in {worst['storm_id']}."
                ),
                evidence_ids=[adverse_id],
                authority=EvidenceAuthority.DERIVED_EXACT,
                source_ids=[baseline_run["run_id"], alternative_run["run_id"]],
                value=float(worst["delta_cfs"]),
                units="cfs",
                run_ids=[baseline_run["run_id"], alternative_run["run_id"]],
                metric_id="outfall_peak_delta_cfs",
                object_id="OUT1",
                derivation="alternative OUT1 peak flow minus matching baseline OUT1 peak flow",
                exact=True,
                created_at=created_at,
            )
        )

    def _append_repository_run_evidence(
        self,
        project_id: str,
        run_id: str,
        references: list[AssistantEvidenceReference],
        claims: list[EvidenceClaim],
        created_at: str,
    ) -> None:
        run = self._repository_run(project_id, run_id)
        references.append(
            AssistantEvidenceReference(
                evidence_id=f"exact-run:{run_id}",
                kind="exact_run",
                label="Pinned exact SWMM execution",
                source_id=run_id,
            )
        )
        for metric in run["metric_records"]:
            evidence_id = (
                f"exact-metric:{run_id}:{metric['metric_id']}:{metric['object_id'] or 'system'}"
            )
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="exact_metric",
                    label=str(metric["metric_id"]),
                    source_id=run_id,
                )
            )
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="exact_metric",
                    statement=(
                        f"{metric['metric_id']} is {float(metric['value']):.12g} {metric['units']}."
                    ),
                    evidence_ids=[evidence_id],
                    authority=EvidenceAuthority.EXACT_SWMM,
                    source_ids=[run_id],
                    value=float(metric["value"]),
                    units=str(metric["units"]),
                    run_id=run_id,
                    run_ids=[run_id],
                    metric_id=str(metric["metric_id"]),
                    object_id=str(metric["object_id"]),
                    exact=True,
                    created_at=created_at,
                )
            )

    def _append_repository_alternative_evidence(
        self,
        project_id: str,
        alternative_id: str,
        board: dict[str, Any],
        brief: dict[str, Any] | None,
        references: list[AssistantEvidenceReference],
        claims: list[EvidenceClaim],
        created_at: str,
    ) -> None:
        """Project one complete exact alternative and its matching baseline."""

        baseline = board.get("baseline")
        alternative = next(
            (
                item
                for item in board.get("alternatives", [])
                if item.get("design_id") == alternative_id
            ),
            None,
        )
        if baseline is None or alternative is None:
            raise ValueError(
                "the selected alternative does not have a complete exact baseline comparison"
            )
        expected_storms = set(board.get("storm_ids", []))
        if (
            not expected_storms
            or set(baseline.get("job_ids", {})) != expected_storms
            or set(alternative.get("job_ids", {})) != expected_storms
        ):
            raise ValueError(
                "the selected alternative is missing one or more exact storm executions"
            )

        alternative_evidence_id = f"alternative:{alternative_id}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=alternative_evidence_id,
                kind="alternative",
                label="Selected exact alternative",
                source_id=alternative_id,
            )
        )
        for design, role in ((baseline, "Baseline"), (alternative, "Alternative")):
            for storm_id in sorted(expected_storms):
                run_id = str(design["job_ids"][storm_id])
                run = self._repository_run(project_id, run_id)
                run_evidence_id = f"exact-run:{run_id}"
                references.append(
                    AssistantEvidenceReference(
                        evidence_id=run_evidence_id,
                        kind="exact_run",
                        label=f"{role} exact run · selected storm",
                        source_id=run_id,
                    )
                )
                for metric in run["metric_records"]:
                    metric_id = str(metric["metric_id"])
                    object_id = str(metric["object_id"] or "system")
                    evidence_id = f"exact-metric:{run_id}:{metric_id}:{object_id}"
                    references.append(
                        AssistantEvidenceReference(
                            evidence_id=evidence_id,
                            kind="exact_metric",
                            label=f"{role} {metric_id} · selected storm",
                            source_id=run_id,
                        )
                    )
                    claims.append(
                        self._claim(
                            project_id=project_id,
                            claim_type="exact_metric",
                            statement=(
                                f"{role} {metric_id} for the selected storm is "
                                f"{float(metric['value']):.12g} {metric['units']}."
                            ),
                            evidence_ids=[evidence_id],
                            authority=EvidenceAuthority.EXACT_SWMM,
                            source_ids=[run_id],
                            value=float(metric["value"]),
                            units=str(metric["units"]),
                            run_id=run_id,
                            run_ids=[run_id],
                            metric_id=metric_id,
                            object_id=str(metric["object_id"]),
                            exact=True,
                            created_at=created_at,
                        )
                    )
                qa_evidence_id = f"qa:{run_id}"
                references.append(
                    AssistantEvidenceReference(
                        evidence_id=qa_evidence_id,
                        kind="qa",
                        label=f"{role} execution QA · selected storm",
                        source_id=run_id,
                    )
                )
                claims.append(
                    self._claim(
                        project_id=project_id,
                        claim_type="qa",
                        statement=(
                            f"The {role.lower()} solver job succeeded and retains "
                            f"{len(run['qa_records'])} QA finding records."
                        ),
                        evidence_ids=[qa_evidence_id, run_evidence_id],
                        authority=EvidenceAuthority.QA_RECORD,
                        source_ids=[run_id],
                        created_at=created_at,
                    )
                )

        for summary in alternative.get("metric_summaries", []):
            if summary.get("value") is None:
                continue
            metric_id = str(summary["metric_id"])
            object_id = str(summary.get("object_id") or "system")
            evidence_id = f"derived:{alternative_id}:{metric_id}:{object_id}"
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="derived_metric",
                    label=f"Selected-storm aggregate · {metric_id}",
                    source_id=alternative_id,
                )
            )
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="derived_metric",
                    statement=(
                        f"The {summary['aggregation']} value for {metric_id} is "
                        f"{float(summary['value']):.12g} {summary['units']}."
                    ),
                    evidence_ids=[evidence_id, alternative_evidence_id],
                    authority=EvidenceAuthority.DERIVED_EXACT,
                    source_ids=list(alternative["job_ids"].values()),
                    value=float(summary["value"]),
                    units=str(summary["units"]),
                    run_ids=list(alternative["job_ids"].values()),
                    metric_id=metric_id,
                    object_id=str(summary.get("object_id") or ""),
                    derivation=str(summary["aggregation"]),
                    exact=True,
                    created_at=created_at,
                )
            )

        for change in alternative.get("adverse_peak_changes", []):
            storm_id = str(change["storm_id"])
            metric_id = str(change["metric_id"])
            object_id = str(change.get("object_id") or "system")
            evidence_id = f"derived:{alternative_id}:adverse:{storm_id}:{metric_id}:{object_id}"
            source_runs = [
                str(change["baseline_job_id"]),
                str(change["alternative_job_id"]),
            ]
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="derived_metric",
                    label="Recorded adverse peak change · selected storm",
                    source_id=alternative_id,
                )
            )
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="derived_metric",
                    statement=(
                        f"The selected alternative changes {metric_id} from "
                        f"{float(change['baseline_value']):.12g} to "
                        f"{float(change['alternative_value']):.12g} {change['units']} "
                        f"for the selected storm, an adverse increase of "
                        f"{float(change['delta']):.12g} {change['units']}."
                    ),
                    evidence_ids=[evidence_id, alternative_evidence_id],
                    authority=EvidenceAuthority.DERIVED_EXACT,
                    source_ids=source_runs,
                    value=float(change["delta"]),
                    units=str(change["units"]),
                    run_ids=source_runs,
                    metric_id=metric_id,
                    object_id=str(change.get("object_id") or ""),
                    derivation="alternative peak minus matching baseline peak",
                    exact=True,
                    created_at=created_at,
                )
            )

        self._append_decision_brief_evidence(
            project_id,
            alternative_id,
            brief,
            alternative_evidence_id,
            references,
            claims,
            created_at,
        )

    def _append_decision_brief_evidence(
        self,
        project_id: str,
        alternative_id: str,
        brief: dict[str, Any] | None,
        alternative_evidence_id: str,
        references: list[AssistantEvidenceReference],
        claims: list[EvidenceClaim],
        created_at: str,
    ) -> None:
        if brief is None:
            return
        summary = next(
            (
                item
                for item in brief.get("alternatives", [])
                if item.get("design_id") == alternative_id
            ),
            None,
        )
        for criterion in brief.get("criteria", []):
            criterion_id = str(criterion["criterion_id"])
            evidence_id = f"decision-rule:{criterion_id}"
            references.append(
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="decision_rule",
                    label=str(criterion["label"]),
                    source_id=criterion_id,
                )
            )
            threshold = criterion.get("threshold")
            claims.append(
                self._claim(
                    project_id=project_id,
                    claim_type="decision_rule",
                    statement=(
                        f"The Decision Brief records the {criterion['label']} criterion"
                        + (
                            f" with a threshold of {float(threshold):.12g} "
                            f"{criterion.get('units') or 'unitless'}."
                            if isinstance(threshold, (int, float))
                            else "."
                        )
                    ),
                    evidence_ids=[evidence_id],
                    authority=EvidenceAuthority.DECISION_RULE,
                    source_ids=[str(brief["brief_id"]), criterion_id],
                    value=float(threshold) if isinstance(threshold, (int, float)) else None,
                    units=(
                        str(criterion.get("units") or "unitless")
                        if isinstance(threshold, (int, float))
                        else None
                    ),
                    created_at=created_at,
                )
            )
        if summary is None or summary.get("present_value_cost") is None:
            return
        evidence_id = f"cost-basis:{brief['brief_id']}:{alternative_id}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=evidence_id,
                kind="cost_basis",
                label="Decision Brief present-value cost",
                source_id=str(brief["brief_id"]),
            )
        )
        claims.append(
            self._claim(
                project_id=project_id,
                claim_type="cost",
                statement=(
                    f"The Decision Brief records a present-value cost of "
                    f"{float(summary['present_value_cost']):.12g} {summary['currency']}."
                ),
                evidence_ids=[evidence_id, alternative_evidence_id],
                authority=EvidenceAuthority.SOURCED_INPUT,
                source_ids=[str(brief["brief_id"])],
                value=float(summary["present_value_cost"]),
                units=str(summary["currency"]),
                created_at=created_at,
            )
        )

    def _decision_rule_matrix(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.repository.get_project(packet.project_id)
        if project["project_kind"] == ProjectKind.DEMO.value:
            fixture, _ = self._load_guided_fixture(project)
            alternative = self._guided_alternative(fixture, self._alternative_id(packet, arguments))
            return {
                "alternative_id": alternative["id"],
                "rules": fixture["guided_story"]["rules"],
                "checks": alternative["screening"]["checks"],
                "meets_all_rules": alternative["screening"]["meets_all_rules"],
                "status": alternative["screening"]["status"],
                "limitations": fixture["limitations"],
            }
        if packet.analysis_id is None:
            raise ValueError("decision rules require an analysis")
        brief = self.repository.get_decision_brief(packet.project_id, packet.analysis_id)
        if brief is None:
            return {
                "analysis_id": packet.analysis_id,
                "rules": [],
                "checks": [],
                "status": "not_recorded",
                "limitations": ["No repository-backed Decision Brief is stored for this analysis."],
            }
        alternative_id = str(arguments.get("alternative_id") or packet.anchor.identifier)
        selected = next(
            (item for item in brief["alternatives"] if item["design_id"] == alternative_id),
            None,
        )
        return {
            "analysis_id": packet.analysis_id,
            "alternative_id": alternative_id,
            "rules": brief["criteria"],
            "alternative": selected,
            "status": brief["status"],
            "limitations": brief["limitations"],
        }

    def _compare_exact(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.repository.get_project(packet.project_id)
        if project["project_kind"] == ProjectKind.DEMO.value:
            fixture, _ = self._load_guided_fixture(project)
            alternative = self._guided_alternative(fixture, self._alternative_id(packet, arguments))
            baseline_by_storm = {run["storm_id"]: run for run in fixture["baseline"]["runs"]}
            storms = []
            for run in alternative["storm_runs"]:
                baseline = baseline_by_storm[run["storm_id"]]
                storms.append(
                    {
                        "storm_id": run["storm_id"],
                        "baseline_run_id": baseline["run_id"],
                        "alternative_run_id": run["run_id"],
                        "baseline_outfall_volume_million_gallons": baseline["metrics"][
                            "outfall_volume_million_gallons"
                        ],
                        "alternative_outfall_volume_million_gallons": run[
                            "outfall_volume_million_gallons"
                        ],
                        "baseline_outfall_peak_cfs": run["baseline_outfall_peak_cfs"],
                        "alternative_outfall_peak_cfs": run["outfall_peak_cfs"],
                        "outfall_peak_delta_cfs": run["outfall_peak_delta_cfs"],
                        "qa_status": run["qa_status"],
                    }
                )
            return {
                "alternative_id": alternative["id"],
                "design_id": alternative["design_id"],
                "baseline_four_storm_outfall_volume_million_gallons": fixture["baseline"][
                    "four_storm_outfall_volume_million_gallons"
                ],
                "alternative_four_storm_outfall_volume_million_gallons": alternative[
                    "four_storm_outfall_volume_million_gallons"
                ],
                "outfall_volume_reduction_percent": alternative["outfall_volume_reduction_percent"],
                "storms": storms,
                "screening": alternative["screening"],
                "exact": True,
                "scope": "four selected synthetic storms",
            }
        if packet.analysis_id is None:
            raise ValueError("exact comparison requires an analysis")
        board = AnalysisOrchestrator(self.repository).decision_board(packet.analysis_id)
        alternative_id = str(arguments.get("alternative_id") or packet.anchor.identifier)
        alternative = next(
            (item for item in board["alternatives"] if item["design_id"] == alternative_id),
            None,
        )
        if alternative is None:
            raise KeyError(f"alternative {alternative_id!r} was not found")
        return {
            "analysis_id": packet.analysis_id,
            "baseline": board["baseline"],
            "alternative": alternative,
            "exact": True,
            "blockers": board["blockers"],
        }

    def _runoff_ledger(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.repository.get_project(packet.project_id)
        if project["project_kind"] == ProjectKind.DEMO.value:
            fixture, _ = self._load_guided_fixture(project)
            alternative = self._guided_alternative(fixture, self._alternative_id(packet, arguments))
            storm_id = str(
                arguments.get("storm_id")
                or alternative["screening"]["worst_peak_change"]["storm_id"]
            )
            baseline = next(
                (run for run in fixture["baseline"]["runs"] if run["storm_id"] == storm_id),
                None,
            )
            alternative_run = next(
                (run for run in alternative["storm_runs"] if run["storm_id"] == storm_id),
                None,
            )
            if baseline is None or alternative_run is None:
                raise KeyError(f"storm {storm_id!r} was not found in the selected exact ensemble")
            return {
                "alternative_id": alternative["id"],
                "storm_id": storm_id,
                "baseline_run_id": baseline["run_id"],
                "alternative_run_id": alternative_run["run_id"],
                "baseline": self._guided_runoff_ledger(baseline),
                "alternative": self._guided_runoff_ledger(alternative_run),
                "term_note": (
                    "Infiltration loss is SWMM's runoff-accounting term; it is not measured "
                    "groundwater recharge."
                ),
                "limitations": [
                    "Synthetic test event; modeled, not measured; not annualized; not field evidence."
                ],
            }
        run_id = str(arguments.get("run_id") or packet.anchor.identifier)
        run = self._repository_run(packet.project_id, run_id)
        report = next((item for item in run["artifacts"] if item["role"] == "report"), None)
        if report is None:
            raise ValueError("the selected exact run has no retained report artifact")
        report_path = _safe_fixture_path(self.repository.root, report["relative_path"])
        if not report_path.is_file() or _sha256_bytes(report_path.read_bytes()) != report["sha256"]:
            raise ValueError("the retained report artifact failed its repository hash")
        return {
            "run_id": run_id,
            "ledger": asdict(
                extract_runoff_quantity_ledger(report_path.read_text(errors="replace"))
            ),
            "term_note": (
                "Infiltration loss is SWMM's runoff-accounting term; it is not measured "
                "groundwater recharge."
            ),
        }

    def _resolve_evidence(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        evidence_id = str(arguments.get("evidence_id") or self._anchor_evidence_id(packet.anchor))
        references = [
            item.model_dump(mode="json")
            for item in packet.evidence_references
            if item.evidence_id == evidence_id
        ]
        claims = [
            item.model_dump(mode="json")
            for item in packet.claims
            if evidence_id in item.evidence_ids or item.claim_id == evidence_id
        ]
        if not references and not claims:
            raise KeyError(f"evidence {evidence_id!r} is not present in the authorized packet")
        return {
            "evidence_id": evidence_id,
            "references": references,
            "claims": claims,
            "limitations": packet.limitations,
        }

    def _model_fitness(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        passport = self.repository.get_model_fitness_passport(
            packet.project_id, packet.model_revision_id
        )
        if passport is None:
            return {
                "disposition": "not_assessed",
                "passport": None,
                "limitations": [
                    "No engineer-attested Model Fitness Passport is stored for this revision."
                ],
                "engineer_controlled": True,
            }
        return {
            "disposition": passport["disposition"],
            "intended_use": passport["intended_use"],
            "criteria": [
                {
                    "criterion_id": item["criterion_id"],
                    "label": item["label"],
                    "status": item["status"],
                    "finding": item["finding"],
                    "remediation": item.get("remediation"),
                    "evidence_claim_ids": item.get("evidence_claim_ids", []),
                }
                for item in passport["criteria"]
            ],
            "permitted_outputs": passport["permitted_outputs"],
            "prohibited_uses": passport["prohibited_uses"],
            "reviewer_attested": bool(passport["reviewer_attested"]),
            "limitations": passport["unresolved_limitations"],
            "engineer_controlled": True,
        }

    def _list_limitations(
        self, packet: AssistantEvidencePacket, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        return {
            "limitations": packet.limitations,
            "count": len(packet.limitations),
            "hydraulic_scope_only": True,
            "ecological_benefit_proven": False,
        }

    def _guided_runoff_ledger(self, run: dict[str, Any]) -> dict[str, Any]:
        relative = run.get("artifacts", {}).get("report")
        expected = run.get("report_sha256") or run.get("hashes", {}).get("report_sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise TypeError("guided run does not identify a retained report artifact")
        path = _safe_fixture_path(self.repository.root, f"guided_fixture/{relative}")
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected:
            raise ValueError("guided report artifact failed its pinned SHA-256")
        return {
            "run_id": run["run_id"],
            "report_sha256": expected,
            "ledger": asdict(extract_runoff_quantity_ledger(path.read_text(errors="replace"))),
        }

    def _repository_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        row = self.repository._connection.execute(
            "SELECT j.job_id,j.analysis_id,j.design_id,j.storm_id,j.state,j.physics_cache_key,"
            "a.project_id FROM solver_jobs j JOIN analyses a ON a.analysis_id=j.analysis_id "
            "WHERE j.job_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["project_id"] != project_id or row["state"] != "SUCCEEDED":
            raise KeyError(f"authoritative exact run {run_id!r} was not found")
        metrics = self.repository._connection.execute(
            "SELECT metric_id,object_id,value,units FROM run_metrics WHERE job_id=? "
            "ORDER BY metric_id,object_id",
            (run_id,),
        ).fetchall()
        artifacts = self.repository._connection.execute(
            "SELECT role,sha256,relative_path FROM run_artifacts WHERE job_id=? ORDER BY role",
            (run_id,),
        ).fetchall()
        qa = self.repository._connection.execute(
            "SELECT severity,code,payload_json FROM qa_findings WHERE job_id=? ORDER BY finding_id",
            (run_id,),
        ).fetchall()
        result = dict(row)
        result["metric_records"] = [dict(item) for item in metrics]
        result["artifacts"] = [dict(item) for item in artifacts]
        result["qa_records"] = [
            {**dict(item), "payload": json.loads(item["payload_json"])} for item in qa
        ]
        return result

    @staticmethod
    def _guided_alternative(fixture: dict[str, Any], alternative_id: str) -> dict[str, Any]:
        alternative = next(
            (item for item in fixture["alternatives"] if item["id"] == alternative_id), None
        )
        if alternative is None:
            raise KeyError(f"guided alternative {alternative_id!r} was not found")
        return alternative

    @staticmethod
    def _alternative_id(packet: AssistantEvidencePacket, arguments: dict[str, Any]) -> str:
        alternative_id = arguments.get("alternative_id")
        if alternative_id is None:
            if packet.anchor.kind != "alternative":
                raise ValueError("select an alternative before using this evidence tool")
            alternative_id = packet.anchor.identifier
        return str(alternative_id)

    @staticmethod
    def _anchor_evidence_id(anchor: AssistantEvidenceAnchor) -> str:
        if anchor.kind == "alternative":
            return f"alternative:{anchor.identifier}"
        if anchor.kind == "exact_run":
            return f"exact-run:{anchor.identifier}"
        if anchor.kind == "model_fitness":
            return (
                anchor.identifier
                if anchor.identifier.startswith("model-fitness:")
                else (f"model-fitness:{anchor.identifier}")
            )
        return f"project:{anchor.identifier}"

    @staticmethod
    def _project_limitations(
        project: dict[str, Any],
        passport: dict[str, Any] | None,
        fixture: dict[str, Any] | None,
    ) -> list[str]:
        # Arbitrary project notes/state are intentionally excluded from provider-bound
        # packets. Only typed, reviewed fitness limitations and canonical notices cross
        # this boundary.
        limitations: list[str] = [
            (
                "Hydraulic results are model outputs under documented assumptions; they are not "
                "field truth, regulatory approval, or proof of ecological benefit."
            )
        ]
        if passport:
            limitations.extend(str(item) for item in passport.get("unresolved_limitations", []))
        else:
            limitations.append(
                "No engineer-attested Model Fitness Passport is stored for this revision."
            )
        if fixture:
            limitations.extend(str(item) for item in fixture.get("limitations", []))
            limitations.append(
                "Passing the teaching rule matrix identifies only a candidate for site and "
                "feasibility review; it does not establish design readiness or approval."
            )
            limitations.insert(0, str(fixture["notice"]))
        elif project["project_kind"] == ProjectKind.CONCEPTUAL.value:
            limitations.insert(
                0,
                "Conceptual planning model — not for design, permitting, flood-risk "
                "determination, or representation of an existing drainage system.",
            )
        limitations.append(
            "The retained evidence does not measure water quality, groundwater recharge, "
            "habitat, heat, real-world flooding, maintenance performance, or lifecycle impacts."
        )
        return list(dict.fromkeys(item for item in limitations if item.strip()))

    @staticmethod
    def _claim(**values: Any) -> EvidenceClaim:
        identity = {
            key: values.get(key)
            for key in (
                "project_id",
                "claim_type",
                "statement",
                "evidence_ids",
                "authority",
                "source_ids",
                "value",
                "units",
                "run_id",
                "run_ids",
                "metric_id",
                "object_id",
                "derivation",
                "exact",
            )
        }
        values["claim_id"] = _stable_id("claim", identity)
        return EvidenceClaim(**values)
