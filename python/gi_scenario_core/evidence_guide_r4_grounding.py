"""Preregistered R4 evidence-grounding and prose-truncation experiment.

R4 deliberately asks a narrow question.  Given a synthetic repository packet,
can a model bind the requested metric to the correct evidence record and apply
one explicit threshold rule?  The experiment does not measure hydraulic
validity, ecological benefit, practitioner usefulness, or production safety.

The paid design contains exactly 672 provider calls:

* grounding: 24 scenarios x 4 conditions x 2 models x 2 presentations = 384;
* prose diagnosis: 12 scenarios x 3 conditions x 2 models x 2 presentations
  x 2 output caps = 288.

The two presentations remap every plan/evidence identifier and reorder the
same evidence.  They are repeated measurements, never independent scenarios.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence_evaluation_v2 import (
    ALLOWED_ACTIONS,
    _prohibited_claim_hits,
    extract_quantities,
    unexplained_numeric_tokens,
)
from .evidence_guide_v2_architecture_executor import (
    _AttemptMeter,
    _PostReceiptResourceViolation,
    _safe_error_code,
)
from .evidence_guide_v2_campaign import (
    RANDOM_SEED,
    TIME_BLOCKS,
    AttemptObserver,
    AttemptOutcome,
    AttemptSpec,
    BudgetCaps,
    Corpus,
    ModelPrice,
    canonical_json,
    estimate_provider_budget,
    file_sha256,
    json_sha256,
)
from .evidence_guide_v2_paired import _trace_llm, _weave_turn_receipt_projection
from .inference import (
    InferenceCompletion,
    InferenceUsage,
    build_inference_request_payload,
)

STUDY_VERSION = "gi-evidence-guide-grounding-r4c-final"
PREREGISTRATION_VERSION = "gi-evidence-guide-grounding-preregistration-r4c-final"
OUTPUT_DIRECTORY_NAME = "evidence-guide-r4c-grounding"
GROUNDING_STAGE = "grounding"
PROSE_STAGE = "prose_diagnosis"
STAGES = (GROUNDING_STAGE, PROSE_STAGE)
# Compatibility label only.  Attempt records always use one of STAGES.
STAGE = "r4_grounding_study"
MODELS = (
    "ibm-granite/granite-4.1-8b",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
)
GROUNDING_CONDITIONS = (
    "original_target",
    "target_threshold_crossing",
    "target_withheld",
    "distractor_only_perturbation",
)
PROSE_CONDITIONS = GROUNDING_CONDITIONS[:3]
CONDITIONS = GROUNDING_CONDITIONS
PRESENTATIONS = 2
GROUNDING_MAX_OUTPUT_TOKENS = 128
PROSE_OUTPUT_CAPS = (128, 256)
MAX_OUTPUT_TOKENS = max(PROSE_OUTPUT_CAPS)
R4C_MAX_INPUT_TOKENS = 8192
CORPUS_SPLITS: Mapping[str, tuple[str, ...]] = {
    "development": ("cedar_trace_cc0", "brookside_cc0"),
    "validation": ("pine_ward_cc0", "harbor_annex_cc0"),
}
# Three cases per family; together they balance rule direction and original
# pass/fail state within each family-pair split.
PROSE_SCENARIO_IDS = (
    "r4-ct-01",
    "r4-ct-02",
    "r4-ct-03",
    "r4-bk-08",
    "r4-bk-11",
    "r4-bk-12",
    "r4-pw-13",
    "r4-pw-16",
    "r4-pw-17",
    "r4-ha-19",
    "r4-ha-20",
    "r4-ha-22",
)
GROUNDING_SCHEMA_ID = "r4c-plan-and-evidence-v1"
PROSE_SCHEMA_ID = "r4c-full-prose-v1"
OPERATION_SEQUENCE = (
    "repository_packet",
    "model_interpretation",
    "deterministic_validation",
    "trace_acknowledgement",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEAL_DOMAIN = b"gi-scenario-studio:evidence-guide-r4c-final:\x00"


@dataclass(frozen=True)
class GroundingAttemptSpec(AttemptSpec):
    """One exact schedule cell with its immutable study instance."""

    study_instance_sha256: str = ""

    @property
    def provider_steps(self) -> int:
        return 1


def _strict_json_file(path: Path) -> Mapping[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )
    if not isinstance(value, Mapping):
        raise TypeError("R4 corpus must be a JSON object")
    return value


def _scenario_id(case: Mapping[str, Any]) -> str:
    return str(case.get("scenario_id") or case.get("case_id") or "")


def _condition_map(case: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = case.get("conditions")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TypeError("R4_CONDITIONS_INVALID")
    mapped = {str(row.get("condition_id") or ""): row for row in rows if isinstance(row, Mapping)}
    if tuple(mapped) != GROUNDING_CONDITIONS:
        raise RuntimeError("R4_CONDITION_SET_INVALID")
    return mapped


def _finite_number(value: Any, *, code: str) -> float:
    if isinstance(value, bool):
        raise TypeError(code)
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(code)
    return number


def _rule_result(value: float, operator: str, threshold: float) -> bool:
    if operator == "<=":
        return value <= threshold
    if operator == ">=":
        return value >= threshold
    raise RuntimeError("R4_RULE_OPERATOR_INVALID")


def _claims(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = payload.get("retrieved_evidence")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TypeError("R4_EVIDENCE_LIST_INVALID")
    mapped: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("R4_EVIDENCE_RECORD_INVALID")
        evidence_id = str(row.get("evidence_id") or "")
        if not evidence_id or evidence_id in mapped:
            raise RuntimeError("R4_EVIDENCE_ID_INVALID")
        _finite_number(row.get("value"), code="R4_EVIDENCE_VALUE_NONFINITE")
        if not str(row.get("units") or ""):
            raise RuntimeError("R4_EVIDENCE_UNITS_INVALID")
        mapped[evidence_id] = row
    return mapped


def _target_claim(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    target = payload.get("decision_target")
    if not isinstance(target, Mapping):
        raise TypeError("R4_DECISION_TARGET_INVALID")
    matches = [
        row
        for row in _claims(payload).values()
        if str(row.get("metric") or "") == str(target.get("metric") or "")
        and str(row.get("object_id") or "") == str(target.get("object_id") or "")
    ]
    if len(matches) > 1:
        raise RuntimeError("R4_TARGET_AMBIGUOUS")
    return matches[0] if matches else None


def _verify_case(case: Mapping[str, Any]) -> None:
    cells = _condition_map(case)
    evaluator = case.get("evaluator_only")
    if not isinstance(evaluator, Mapping):
        raise TypeError("R4_EVALUATOR_RECORD_INVALID")
    target_id = str(evaluator.get("target_evidence_id") or "")
    distractor_id = str(evaluator.get("perturbed_distractor_evidence_id") or "")
    payloads = {condition: cells[condition].get("provider_payload") for condition in cells}
    if any(not isinstance(payload, Mapping) for payload in payloads.values()):
        raise RuntimeError("R4_PROVIDER_PAYLOAD_INVALID")
    original = _claims(payloads["original_target"])  # type: ignore[arg-type]
    crossing = _claims(payloads["target_threshold_crossing"])  # type: ignore[arg-type]
    withheld = _claims(payloads["target_withheld"])  # type: ignore[arg-type]
    distractor = _claims(payloads["distractor_only_perturbation"])  # type: ignore[arg-type]
    if (
        not target_id
        or target_id not in original
        or not distractor_id
        or distractor_id not in original
    ):
        raise RuntimeError("R4_INTERVENTION_ID_INVALID")
    if set(crossing) != set(original) or set(distractor) != set(original):
        raise RuntimeError("R4_INTERVENTION_MEMBERSHIP_DRIFT")
    if set(withheld) != set(original) - {target_id}:
        raise RuntimeError("R4_WITHHELD_MEMBERSHIP_INVALID")
    changed_crossing = [key for key in original if original[key] != crossing[key]]
    changed_distractor = [key for key in original if original[key] != distractor[key]]
    if changed_crossing != [target_id] or changed_distractor != [distractor_id]:
        raise RuntimeError("R4_INTERVENTION_NOT_ISOLATED")
    if original[target_id] != distractor[target_id]:
        raise RuntimeError("R4_DISTRACTOR_PERTURBED_TARGET")
    for condition, payload in payloads.items():
        assert isinstance(payload, Mapping)
        visible = canonical_json(payload).decode("utf-8")
        if condition in visible or "evaluator_only" in visible or "expected_plan" in visible:
            raise RuntimeError("R4_CONDITION_LEAKED_TO_PROVIDER")
    rule = payloads["original_target"]["decision_rule"]  # type: ignore[index]
    if not isinstance(rule, Mapping):
        raise TypeError("R4_RULE_INVALID")
    threshold = _finite_number(rule.get("threshold"), code="R4_THRESHOLD_NONFINITE")
    operator = str(rule.get("operator") or "")
    original_result = _rule_result(
        _finite_number(original[target_id]["value"], code="R4_TARGET_NONFINITE"),
        operator,
        threshold,
    )
    crossing_result = _rule_result(
        _finite_number(crossing[target_id]["value"], code="R4_TARGET_NONFINITE"),
        operator,
        threshold,
    )
    if original_result == crossing_result:
        raise RuntimeError("R4_TARGET_DID_NOT_CROSS_THRESHOLD")


def load_r4_corpus(path: Path) -> Corpus:
    """Load and independently verify the checked-in R4 corpus.

    The campaign split names are retained for cloud-adapter compatibility, but
    they denote two project-family pairs rather than random train/test rows.
    The judge-calibration split is intentionally empty and receives no calls.
    """

    path = path.resolve()
    raw = _strict_json_file(path)
    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
        raise TypeError("R4_SCENARIOS_INVALID")
    normalized: list[Mapping[str, Any]] = []
    for row in scenarios:
        if not isinstance(row, Mapping):
            raise TypeError("R4_SCENARIO_INVALID")
        scenario_id = _scenario_id(row)
        if not scenario_id:
            raise RuntimeError("R4_SCENARIO_ID_INVALID")
        normalized_row = {**deepcopy(dict(row)), "case_id": scenario_id}
        _verify_case(normalized_row)
        normalized.append(normalized_row)
    if len(normalized) != 24 or len({_scenario_id(row) for row in normalized}) != 24:
        raise RuntimeError("R4_SCENARIO_COUNT_INVALID")
    body = {key: value for key, value in raw.items() if key != "manifest"}
    manifest = raw.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("corpus_body_sha256") != json_sha256(body):
        raise RuntimeError("R4_CORPUS_MANIFEST_INVALID")
    split_rows = {
        split: tuple(row for row in normalized if str(row.get("project_family")) in families)
        for split, families in CORPUS_SPLITS.items()
    }
    if any(len(rows) != 12 for rows in split_rows.values()):
        raise RuntimeError("R4_PROJECT_FAMILY_SPLIT_INVALID")
    split_rows["judge_calibration"] = ()
    return Corpus(
        corpus_id=str(raw.get("dataset_id") or ""),
        corpus_version=str(raw.get("dataset_version") or ""),
        manifest_sha256=file_sha256(path),
        split_sha256={split: json_sha256(list(rows)) for split, rows in split_rows.items()},
        splits=split_rows,
        source_paths={split: str(path) for split in split_rows},
        datasets={split: raw for split in split_rows},
        split_counts={split: len(rows) for split, rows in split_rows.items()},
    )


def _all_cases(corpus: Corpus) -> tuple[Mapping[str, Any], ...]:
    return tuple(row for split in CORPUS_SPLITS for row in corpus.splits[split])


def _alias_maps(
    case: Mapping[str, Any], *, presentation: int
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
    cells = _condition_map(case)
    original = cells["original_target"]["provider_payload"]
    if not isinstance(original, Mapping):
        raise TypeError("R4_PROVIDER_PAYLOAD_INVALID")
    rule = original.get("decision_rule")
    if not isinstance(rule, Mapping):
        raise TypeError("R4_RULE_INVALID")
    original_plan_ids = sorted(
        str(rule[key])
        for key in (
            "if_rule_met_plan_id",
            "if_rule_not_met_plan_id",
            "if_evidence_missing_plan_id",
        )
    )
    evidence_ids = tuple(_claims(original))
    seed = f"{STUDY_VERSION}:{_scenario_id(case)}:{presentation}"
    plan_aliases = {
        value: "plan_" + hashlib.sha256(f"{seed}:plan:{value}".encode()).hexdigest()[:16]
        for value in original_plan_ids
    }
    evidence_aliases = {
        value: "ev_" + hashlib.sha256(f"{seed}:evidence:{value}".encode()).hexdigest()[:16]
        for value in evidence_ids
    }
    order = list(evidence_ids)
    random.Random(int(hashlib.sha256(f"{seed}:order".encode()).hexdigest()[:16], 16)).shuffle(order)
    return plan_aliases, evidence_aliases, tuple(order)


def _project_payload(
    case: Mapping[str, Any], *, condition: str, presentation: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return model-visible payload and evaluator projection.

    Condition labels, evaluator records, original identifiers, and expected
    plans never enter the model-visible return value.
    """

    cell = _condition_map(case)[condition]
    source = cell.get("provider_payload")
    if not isinstance(source, Mapping):
        raise TypeError("R4_PROVIDER_PAYLOAD_INVALID")
    payload = deepcopy(dict(source))
    plan_aliases, evidence_aliases, order = _alias_maps(case, presentation=presentation)
    rule = payload.get("decision_rule")
    if not isinstance(rule, dict):
        raise TypeError("R4_RULE_INVALID")
    for key in (
        "if_rule_met_plan_id",
        "if_rule_not_met_plan_id",
        "if_evidence_missing_plan_id",
    ):
        rule[key] = plan_aliases[str(rule[key])]
    response_contract = payload.get("response_contract")
    if isinstance(response_contract, dict):
        response_contract["plan_id_enum"] = sorted(plan_aliases.values())
    source_claims = _claims(payload)
    reordered: list[dict[str, Any]] = []
    for original_id in order:
        if original_id not in source_claims:
            continue
        claim = deepcopy(dict(source_claims[original_id]))
        claim["evidence_id"] = evidence_aliases[original_id]
        reordered.append(claim)
    payload["retrieved_evidence"] = reordered
    serialized = canonical_json(payload).decode("utf-8")
    if (
        condition in serialized
        or "evaluator_only" in serialized
        or "expected_plan" in serialized
        or any(original in serialized for original in (*plan_aliases, *evidence_aliases))
    ):
        raise RuntimeError("R4_MODEL_VISIBLE_SOURCE_BOUNDARY_FAILED")
    evaluator = case.get("evaluator_only")
    if not isinstance(evaluator, Mapping):
        raise TypeError("R4_EVALUATOR_RECORD_INVALID")
    original_target_id = str(evaluator["target_evidence_id"])
    expected_original = str(evaluator["expected_plan_by_condition"][condition])
    projection = {
        "condition": condition,
        "target_original_id": original_target_id,
        "target_alias": evidence_aliases[original_target_id],
        "expected_plan_alias": plan_aliases[expected_original],
        "plan_aliases": plan_aliases,
        "evidence_aliases": evidence_aliases,
        "presentation_order_original_ids": list(order),
    }
    return payload, projection


def _time_block(
    *,
    stage: str,
    case_ordinal: int,
    presentation: int,
    model_id: str,
    condition: str,
) -> str:
    """Assign an exactly balanced blocked Latin-square execution period.

    Hashing each cell independently made every condition appear in every time
    block, but did not balance the counts.  Provider drift could therefore be
    mistaken for an evidence-condition effect.  The deterministic base below
    cycles evenly within every split/model stratum; condition offsets then
    rotate the matched cells.  The output-token cap is intentionally absent so
    each 128/256 prose pair executes in the same block.
    """

    if model_id not in MODELS or case_ordinal < 0 or not 0 <= presentation < PRESENTATIONS:
        raise ValueError("R4 time-block factors are invalid")
    conditions = GROUNDING_CONDITIONS if stage == GROUNDING_STAGE else PROSE_CONDITIONS
    if stage not in STAGES or condition not in conditions:
        raise ValueError("R4 time-block condition is invalid")
    base = (case_ordinal * PRESENTATIONS + presentation + MODELS.index(model_id)) % len(TIME_BLOCKS)
    condition_offset = conditions.index(condition) % len(TIME_BLOCKS)
    return TIME_BLOCKS[(base + condition_offset) % len(TIME_BLOCKS)]


def build_schedule(corpus: Corpus, *, instance_sha256: str) -> tuple[GroundingAttemptSpec, ...]:
    if not _SHA256.fullmatch(instance_sha256):
        raise ValueError("R4 study instance must be a lowercase SHA-256")
    specs: list[GroundingAttemptSpec] = []
    prose_ids = set(PROSE_SCENARIO_IDS)
    for split in CORPUS_SPLITS:
        cases = sorted(corpus.splits[split], key=_scenario_id)
        prose_case_ordinals = {
            _scenario_id(case): ordinal
            for ordinal, case in enumerate(
                case for case in cases if _scenario_id(case) in prose_ids
            )
        }
        for case_ordinal, case in enumerate(cases):
            case_id = _scenario_id(case)
            family = str(case["project_family"])
            for presentation in range(PRESENTATIONS):
                for model_id in MODELS:
                    for condition in GROUNDING_CONDITIONS:
                        factors = {
                            "study_instance_sha256": instance_sha256,
                            "stage": GROUNDING_STAGE,
                            "split": split,
                            "case_id": case_id,
                            "project_family": family,
                            "model_id": model_id,
                            "architecture_id": "R4-grounding",
                            "prompt_id": GROUNDING_SCHEMA_ID,
                            "max_output_tokens": GROUNDING_MAX_OUTPUT_TOKENS,
                            "repeat": presentation,
                            "presentation_index": presentation,
                            "ablation_condition": condition,
                        }
                        specs.append(
                            GroundingAttemptSpec(
                                attempt_id=json_sha256(factors)[:32],
                                time_block=_time_block(
                                    stage=GROUNDING_STAGE,
                                    case_ordinal=case_ordinal,
                                    presentation=presentation,
                                    model_id=model_id,
                                    condition=condition,
                                ),
                                hidden=False,
                                judge_role=None,
                                order=None,
                                **factors,
                            )
                        )
                if case_id not in prose_ids:
                    continue
                for model_id in MODELS:
                    for condition in PROSE_CONDITIONS:
                        for cap in PROSE_OUTPUT_CAPS:
                            factors = {
                                "study_instance_sha256": instance_sha256,
                                "stage": PROSE_STAGE,
                                "split": split,
                                "case_id": case_id,
                                "project_family": family,
                                "model_id": model_id,
                                "architecture_id": "R4-prose",
                                "prompt_id": PROSE_SCHEMA_ID,
                                "max_output_tokens": cap,
                                "repeat": presentation,
                                "presentation_index": presentation,
                                "ablation_condition": condition,
                            }
                            specs.append(
                                GroundingAttemptSpec(
                                    attempt_id=json_sha256(factors)[:32],
                                    time_block=_time_block(
                                        stage=PROSE_STAGE,
                                        case_ordinal=prose_case_ordinals[case_id],
                                        presentation=presentation,
                                        model_id=model_id,
                                        condition=condition,
                                    ),
                                    hidden=False,
                                    judge_role=None,
                                    order=None,
                                    **factors,
                                )
                            )
    random.Random(RANDOM_SEED).shuffle(specs)
    counts = Counter(spec.stage for spec in specs)
    identifiers = [spec.attempt_id for spec in specs]
    if counts != Counter({GROUNDING_STAGE: 384, PROSE_STAGE: 288}):
        raise RuntimeError("R4_SCHEDULE_FACTOR_COUNT_INVALID")
    if len(specs) != 672 or len(identifiers) != len(set(identifiers)):
        raise RuntimeError("R4_SCHEDULE_IDENTITY_INVALID")
    grounding_blocks: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    prose_blocks: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    prose_cap_pairs: dict[tuple[str, str, str, int, str], dict[int, str]] = defaultdict(dict)
    for spec in specs:
        if spec.stage == GROUNDING_STAGE:
            grounding_blocks[
                (spec.split, spec.case_id, spec.model_id, spec.presentation_index)
            ].add(spec.time_block)
        else:
            prose_blocks[(spec.split, spec.case_id, spec.model_id, spec.presentation_index)].add(
                spec.time_block
            )
            prose_cap_pairs[
                (
                    spec.split,
                    spec.case_id,
                    spec.model_id,
                    spec.presentation_index,
                    str(spec.ablation_condition),
                )
            ][spec.max_output_tokens] = spec.time_block
    if len(grounding_blocks) != 96 or any(
        blocks != set(TIME_BLOCKS) for blocks in grounding_blocks.values()
    ):
        raise RuntimeError("R4_GROUNDING_TIME_BLOCK_ROTATION_INVALID")
    if len(prose_blocks) != 48 or any(
        blocks != set(TIME_BLOCKS) for blocks in prose_blocks.values()
    ):
        raise RuntimeError("R4_PROSE_TIME_BLOCK_ROTATION_INVALID")
    if len(prose_cap_pairs) != 144 or any(
        set(pair) != set(PROSE_OUTPUT_CAPS) or len(set(pair.values())) != 1
        for pair in prose_cap_pairs.values()
    ):
        raise RuntimeError("R4_PROSE_CAP_TIME_BLOCK_PAIR_INVALID")
    stage_block_counts = Counter((spec.stage, spec.time_block) for spec in specs)
    if stage_block_counts != Counter(
        {(GROUNDING_STAGE, block): 128 for block in TIME_BLOCKS}
        | {(PROSE_STAGE, block): 96 for block in TIME_BLOCKS}
    ):
        raise RuntimeError("R4_TIME_BLOCK_TOTAL_BALANCE_INVALID")
    grounding_strata = Counter(
        (
            spec.split,
            spec.model_id,
            str(spec.ablation_condition),
            spec.time_block,
        )
        for spec in specs
        if spec.stage == GROUNDING_STAGE
    )
    if set(grounding_strata.values()) != {8} or len(grounding_strata) != 48:
        raise RuntimeError("R4_GROUNDING_TIME_BLOCK_STRATUM_BALANCE_INVALID")
    prose_strata = Counter(
        (
            spec.split,
            spec.model_id,
            str(spec.ablation_condition),
            spec.time_block,
        )
        for spec in specs
        if spec.stage == PROSE_STAGE
    )
    if set(prose_strata.values()) != {8} or len(prose_strata) != 36:
        raise RuntimeError("R4_PROSE_TIME_BLOCK_STRATUM_BALANCE_INVALID")
    return tuple(specs)


def schedule_records(specs: Sequence[AttemptSpec]) -> list[dict[str, Any]]:
    return [asdict(spec) for spec in specs]


def _assert_provider_schema_supported(schema: Mapping[str, Any]) -> None:
    """Enforce the W&B Inference structured-output subset used by R4b.

    R4 was stopped after the gateway rejected ``uniqueItems`` before either
    model executed. Array uniqueness remains a repository-owned validation
    rule in :func:`_parse_response`; it is intentionally not delegated to the
    provider grammar.
    """

    if set(schema) != {"type", "additionalProperties", "properties", "required"}:
        raise RuntimeError("R4C_PROVIDER_SCHEMA_TOP_LEVEL_UNSUPPORTED")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise TypeError("R4C_PROVIDER_SCHEMA_PROPERTIES_INVALID")
    for descriptor in properties.values():
        if not isinstance(descriptor, Mapping):
            raise TypeError("R4C_PROVIDER_SCHEMA_DESCRIPTOR_INVALID")
        if not set(descriptor).issubset({"type", "enum", "items"}):
            raise RuntimeError("R4C_PROVIDER_SCHEMA_KEY_UNSUPPORTED")
        items = descriptor.get("items")
        if items is not None and (
            not isinstance(items, Mapping) or set(items) != {"type"}
        ):
            raise RuntimeError("R4C_PROVIDER_SCHEMA_ITEMS_UNSUPPORTED")


def _response_schema(stage: str, plan_ids: Sequence[str]) -> dict[str, Any]:
    plan = {"type": "string", "enum": sorted(str(item) for item in plan_ids)}
    # W&B Inference currently rejects JSON Schema ``uniqueItems``. Duplicate
    # identifiers are still rejected deterministically after provider parsing.
    strings = {"type": "array", "items": {"type": "string"}}
    if stage == GROUNDING_STAGE:
        properties: dict[str, Any] = {
            "plan_id": plan,
            "evidence_ids": strings,
        }
    elif stage == PROSE_STAGE:
        properties = {
            "plan_id": plan,
            "response_mode": {"type": "string", "enum": ["answer", "refuse"]},
            "answer_text": {"type": "string"},
            "evidence_ids": strings,
            "limitation_ids": strings,
            "action_ids": strings,
        }
    else:
        raise RuntimeError("R4_STAGE_INVALID")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }
    _assert_provider_schema_supported(schema)
    return schema


def _prose_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    target = _target_claim(payload)
    if target is None:
        limitations = ["modeled_not_measured", "synthetic_fixture"]
        action = "review_limitations"
        response_mode = "refuse"
    else:
        limitations = sorted(str(item) for item in target.get("limitation_ids", ()))
        action = "open_evidence"
        response_mode = "answer"
    if action not in ALLOWED_ACTIONS:
        raise RuntimeError("R4_PROSE_ACTION_INVALID")
    return {
        "response_mode": response_mode,
        "required_limitation_ids": limitations,
        "required_action_ids": [action],
        "answer_text_rule": (
            "Write at most two short sentences. Use only the cited target evidence and the "
            "stated rule. Do not introduce any other number, recommendation, approval, field "
            "claim, ecological claim, or regulatory conclusion."
        ),
    }


def _model_request(
    *, spec: AttemptSpec, payload: Mapping[str, Any]
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    visible = deepcopy(dict(payload))
    plans = visible["decision_rule"]
    plan_ids = (
        plans["if_rule_met_plan_id"],
        plans["if_rule_not_met_plan_id"],
        plans["if_evidence_missing_plan_id"],
    )
    if spec.stage == PROSE_STAGE:
        visible["prose_response_policy"] = _prose_policy(visible)
        system = (
            "Apply exactly one supplied synthetic screening rule to the matching target "
            "evidence. Distractor records may be present. Return the strict full-prose JSON "
            "contract. Cite only the target record used; if it is absent, refuse. Never "
            "approve, rank, recommend, predict, or claim measured field or ecological effects."
        )
    elif spec.stage == GROUNDING_STAGE:
        system = (
            "Apply exactly one supplied synthetic screening rule to the matching target "
            "evidence. Distractor records may be present. Return only plan_id and the exact "
            "target evidence_ids in the strict JSON contract; if the target is absent, use "
            "the missing-evidence plan and an empty evidence_ids list."
        )
    else:
        raise RuntimeError("R4_STAGE_INVALID")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json(visible).decode("utf-8")},
    ]
    schema = _response_schema(spec.stage, plan_ids)
    serialized = canonical_json({"messages": messages, "schema": schema}).decode("utf-8")
    condition = str(spec.ablation_condition or "")
    if (
        condition in serialized
        or "evaluator_only" in serialized
        or "expected_plan" in serialized
        or "condition_id" in serialized
    ):
        raise RuntimeError("R4_CONDITION_VISIBLE_TO_MODEL")
    return messages, schema


def _expected_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    target = _target_claim(payload)
    rule = payload.get("decision_rule")
    if not isinstance(rule, Mapping):
        raise TypeError("R4_RULE_INVALID")
    threshold = _finite_number(rule.get("threshold"), code="R4_THRESHOLD_NONFINITE")
    operator = str(rule.get("operator") or "")
    if target is None:
        return {
            "plan_id": str(rule["if_evidence_missing_plan_id"]),
            "evidence_ids": [],
            **_prose_policy(payload),
        }
    result = _rule_result(
        _finite_number(target.get("value"), code="R4_TARGET_NONFINITE"),
        operator,
        threshold,
    )
    return {
        "plan_id": str(rule["if_rule_met_plan_id"] if result else rule["if_rule_not_met_plan_id"]),
        "evidence_ids": [str(target["evidence_id"])],
        **_prose_policy(payload),
    }


def d1_positive_control(corpus: Corpus) -> dict[str, Any]:
    """Evaluate every projected grounding cell with the deterministic resolver."""

    rows: list[dict[str, Any]] = []
    for split in CORPUS_SPLITS:
        for case in sorted(corpus.splits[split], key=_scenario_id):
            evaluator = case.get("evaluator_only")
            if not isinstance(evaluator, Mapping):
                raise TypeError("R4_EVALUATOR_RECORD_INVALID")
            for presentation in range(PRESENTATIONS):
                for condition in GROUNDING_CONDITIONS:
                    payload, projection = _project_payload(
                        case, condition=condition, presentation=presentation
                    )
                    expected = _expected_output(payload)
                    passed = bool(
                        expected["plan_id"] == projection["expected_plan_alias"]
                        and expected["evidence_ids"]
                        == ([] if condition == "target_withheld" else [projection["target_alias"]])
                    )
                    rows.append(
                        {
                            "split": split,
                            "case_id": _scenario_id(case),
                            "presentation": presentation,
                            "condition": condition,
                            "passed": passed,
                            "model_visible_payload_sha256": json_sha256(payload),
                        }
                    )
    if len(rows) != 192 or not all(row["passed"] for row in rows):
        raise RuntimeError("R4_D1_POSITIVE_CONTROL_FAILED")
    body = {
        "gate_id": "r4c-d1-deterministic-positive-control-v1",
        "cell_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "all_passed": True,
        "rows_sha256": json_sha256(rows),
    }
    return {**body, "gate_sha256": json_sha256(body)}


def _parse_response(
    content: str, *, schema: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None, ("response_not_json",)
    properties = schema.get("properties")
    required = schema.get("required")
    if (
        not isinstance(value, dict)
        or not isinstance(properties, Mapping)
        or not isinstance(required, list)
    ):
        return None, ("response_schema_invalid",)
    errors: list[str] = []
    if set(value) != set(required):
        errors.append("response_keys_mismatch")
    for key, descriptor in properties.items():
        if not isinstance(descriptor, Mapping):
            errors.append("response_schema_invalid")
            continue
        item = value.get(key)
        if descriptor.get("type") == "string" and not isinstance(item, str):
            errors.append(f"{key}_type_invalid")
        if descriptor.get("type") == "array":
            if not isinstance(item, list) or any(
                not isinstance(entry, str) for entry in item
            ):
                errors.append(f"{key}_type_invalid")
            elif len(item) != len(set(item)):
                errors.append(f"{key}_duplicate")
        options = descriptor.get("enum")
        if options is not None and item not in options:
            errors.append(f"{key}_enum_invalid")
    return value, tuple(sorted(set(errors)))


def _validate_output(
    *,
    stage: str,
    payload: Mapping[str, Any],
    parsed: Mapping[str, Any] | None,
    protocol_errors: Sequence[str],
) -> dict[str, Any]:
    expected = _expected_output(payload)
    provider_response = parsed is not None
    protocol_valid = bool(provider_response and not protocol_errors)
    selected_plan = str(parsed.get("plan_id") or "") if parsed else ""
    decision_fidelity = bool(protocol_valid and selected_plan == expected["plan_id"])
    observed_ids = parsed.get("evidence_ids") if parsed else None
    evidence_binding = bool(
        protocol_valid
        and isinstance(observed_ids, list)
        and observed_ids == expected["evidence_ids"]
    )
    semantic_validation: bool | None = None
    semantic_errors: list[str] = []
    if stage == PROSE_STAGE:
        semantic_validation = protocol_valid
        if parsed is None:
            semantic_validation = False
        else:
            if parsed.get("response_mode") != expected["response_mode"]:
                semantic_errors.append("response_mode_mismatch")
            if parsed.get("limitation_ids") != expected["required_limitation_ids"]:
                semantic_errors.append("limitation_ids_mismatch")
            if parsed.get("action_ids") != expected["required_action_ids"]:
                semantic_errors.append("action_ids_mismatch")
            answer = parsed.get("answer_text")
            if not isinstance(answer, str):
                semantic_errors.append("answer_text_invalid")
            else:
                semantic_errors.extend(_prohibited_claim_hits(answer))
                allowed: set[tuple[Decimal, str]] = set()
                target = _target_claim(payload)
                if target is not None:
                    allowed.add((Decimal(str(target["value"])), str(target["units"])))
                    rule = payload["decision_rule"]
                    allowed.add((Decimal(str(rule["threshold"])), str(rule["units"])))
                observed = {
                    (Decimal(str(item["value"])), str(item["units"]))
                    for item in extract_quantities(answer)
                }
                if not observed.issubset(allowed) or unexplained_numeric_tokens(answer):
                    semantic_errors.append("prose_quantity_unsupported")
        semantic_validation = bool(semantic_validation and not semantic_errors)
    validation_errors = sorted(
        set(
            list(protocol_errors)
            + ([] if decision_fidelity else ["decision_plan_mismatch"])
            + ([] if evidence_binding else ["evidence_binding_mismatch"])
            + semantic_errors
        )
    )
    provisional_conformance = bool(
        provider_response
        and protocol_valid
        and decision_fidelity
        and evidence_binding
        and semantic_validation is not False
    )
    return {
        "provider_response_received": provider_response,
        "protocol_valid": protocol_valid,
        "decision_fidelity": decision_fidelity,
        "evidence_binding": evidence_binding,
        "semantic_validation": semantic_validation,
        "provisional_model_conformance": provisional_conformance,
        "expected_plan_id": expected["plan_id"],
        "expected_evidence_ids": expected["evidence_ids"],
        "selected_plan_id": selected_plan or None,
        "errors": validation_errors,
    }


def _intervention_projection(
    case: Mapping[str, Any], *, condition: str, presentation: int
) -> dict[str, Any]:
    original, original_projection = _project_payload(
        case, condition="original_target", presentation=presentation
    )
    current, current_projection = _project_payload(
        case, condition=condition, presentation=presentation
    )
    evaluator = case["evaluator_only"]
    target_original = str(evaluator["target_evidence_id"])
    distractor_original = str(evaluator["perturbed_distractor_evidence_id"])
    original_claims = _claims(original)
    current_claims = _claims(current)
    target_alias = original_projection["evidence_aliases"][target_original]
    distractor_alias = original_projection["evidence_aliases"][distractor_original]
    return {
        "condition": condition,
        "contract": str(evaluator["intervention_contract"][condition]),
        "target_alias": target_alias,
        "distractor_alias": distractor_alias,
        "original_target_record": deepcopy(original_claims.get(target_alias)),
        "current_target_record": deepcopy(current_claims.get(target_alias)),
        "original_distractor_record": deepcopy(original_claims.get(distractor_alias)),
        "current_distractor_record": deepcopy(current_claims.get(distractor_alias)),
        "current_evidence_ids": list(current_claims),
        "presentation_order_original_ids_sha256": json_sha256(
            current_projection["presentation_order_original_ids"]
        ),
    }


def _experimental_receipt(
    *,
    spec: GroundingAttemptSpec,
    schedule_sha256: str,
    d1_gate_sha256: str,
    case: Mapping[str, Any],
    payload: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    rule = payload["decision_rule"]
    target = payload["decision_target"]
    normalized_request = {
        "model": spec.model_id,
        "messages": [dict(item) for item in messages],
        "response_schema": deepcopy(dict(schema)),
        "seed": RANDOM_SEED,
        "temperature": 0,
        "max_output_tokens": spec.max_output_tokens,
    }
    model_visible_contract = {
        "messages": normalized_request["messages"],
        "response_schema": normalized_request["response_schema"],
        "seed": RANDOM_SEED,
        "temperature": 0,
    }
    body = {
        "schema_version": "1.0",
        "receipt_version": "r4c-full-request-intervention-receipt-v1",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": spec.study_instance_sha256,
        "schedule_sha256": schedule_sha256,
        "schedule_cell_sha256": json_sha256(asdict(spec)),
        "attempt_id": spec.attempt_id,
        "stage": spec.stage,
        "split": spec.split,
        "case_id_sha256": hashlib.sha256(spec.case_id.encode()).hexdigest(),
        "project_family_sha256": hashlib.sha256(spec.project_family.encode()).hexdigest(),
        "model_id": spec.model_id,
        "presentation": spec.presentation_index,
        "condition": spec.ablation_condition,
        "operator": str(rule["operator"]),
        "threshold": _finite_number(rule["threshold"], code="R4_THRESHOLD_NONFINITE"),
        "rule_units": str(rule["units"]),
        "decision_target": deepcopy(dict(target)),
        "intervention": _intervention_projection(
            case,
            condition=str(spec.ablation_condition),
            presentation=spec.presentation_index,
        ),
        "normalized_request": normalized_request,
        "normalized_request_sha256": json_sha256(normalized_request),
        "model_visible_contract_sha256": json_sha256(model_visible_contract),
        "d1_positive_control_gate_sha256": d1_gate_sha256,
        "condition_label_visible_to_model": False,
        "source_boundary": "provider_payload_only_no_evaluator_gold_or_condition_label",
        "seal_kind": "domain_separated_sha256_content_seal_not_identity_signature",
    }
    digest = json_sha256(body)
    seal = hashlib.sha256(_SEAL_DOMAIN + canonical_json(body)).hexdigest()
    return {**body, "body_sha256": digest, "integrity_seal_sha256": seal}


def validate_experimental_receipt(receipt: Mapping[str, Any]) -> bool:
    body = {
        key: value
        for key, value in receipt.items()
        if key not in {"body_sha256", "integrity_seal_sha256"}
    }
    return bool(
        receipt.get("condition_label_visible_to_model") is False
        and receipt.get("body_sha256") == json_sha256(body)
        and receipt.get("integrity_seal_sha256")
        == hashlib.sha256(_SEAL_DOMAIN + canonical_json(body)).hexdigest()
        and _SHA256.fullmatch(str(receipt.get("study_instance_sha256") or ""))
        and _SHA256.fullmatch(str(receipt.get("schedule_sha256") or ""))
    )


# Backward-compatible name used by prior diagnostics.
validate_semantic_receipt = validate_experimental_receipt


class GroundingExecutor:
    """Execute one frozen cell without conflating model and trace outcomes."""

    def __init__(
        self,
        *,
        corpus: Corpus,
        provider: Any,
        entity: str,
        project: str,
        pricing: Mapping[str, ModelPrice],
        execution_mode: str,
        study_instance_sha256: str | None = None,
    ) -> None:
        if execution_mode not in {"fake", "live"}:
            raise ValueError("R4 execution mode is invalid")
        self.corpus = corpus
        self.provider = provider
        self.entity = entity
        self.project = project
        self.pricing = dict(pricing)
        self.execution_mode = execution_mode
        self.study_instance_sha256 = study_instance_sha256
        self.d1_gate = d1_positive_control(corpus)
        self._schedule_sha256: str | None = None

    def _schedule_hash(self, spec: GroundingAttemptSpec) -> str:
        instance = self.study_instance_sha256 or spec.study_instance_sha256
        if instance != spec.study_instance_sha256:
            raise RuntimeError("R4_EXECUTOR_INSTANCE_MISMATCH")
        if self._schedule_sha256 is None:
            self._schedule_sha256 = json_sha256(
                schedule_records(build_schedule(self.corpus, instance_sha256=instance))
            )
        return self._schedule_sha256

    def execute(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        if not isinstance(spec, GroundingAttemptSpec):
            raise TypeError("R4_ATTEMPT_SPEC_TYPE_INVALID")
        if spec.stage not in STAGES or spec.model_id not in self.pricing:
            raise RuntimeError("R4_ATTEMPT_SPEC_INVALID")
        if spec.ablation_condition not in (
            GROUNDING_CONDITIONS if spec.stage == GROUNDING_STAGE else PROSE_CONDITIONS
        ):
            raise RuntimeError("R4_ATTEMPT_CONDITION_INVALID")
        price = self.pricing[spec.model_id]
        meter = _AttemptMeter(
            self.provider,
            max_output_tokens=spec.max_output_tokens,
            max_input_tokens=R4C_MAX_INPUT_TOKENS,
        )
        started = time.perf_counter()
        completion: InferenceCompletion | None = None
        parsed: dict[str, Any] | None = None
        protocol_errors: tuple[str, ...] = ()
        validation: dict[str, Any] | None = None
        receipt: dict[str, Any] = {}
        trace: Any = None
        trace_fault: str | None = None
        preparation_fault: str | None = None
        post_receipt_resource_violation: dict[str, Any] | None = None
        payload: dict[str, Any] = {}
        try:
            payload, _projection = _project_payload(
                case,
                condition=str(spec.ablation_condition),
                presentation=spec.presentation_index,
            )
            messages, schema = _model_request(spec=spec, payload=payload)
            receipt = _experimental_receipt(
                spec=spec,
                schedule_sha256=self._schedule_hash(spec),
                d1_gate_sha256=str(self.d1_gate["gate_sha256"]),
                case=case,
                payload=payload,
                messages=messages,
                schema=schema,
            )
            try:
                with observer.attempt(spec, payload) as trace:
                    call_id = "r4-repository-" + receipt["body_sha256"][:20]
                    if trace is not None and hasattr(trace, "tool"):
                        with trace.tool(
                            name="repository.get_synthetic_evidence_packet",
                            arguments={},
                            tool_call_id=call_id,
                        ) as handle:
                            handle.record({"result_sha256": json_sha256(payload)})
                    with _trace_llm(trace, model=spec.model_id, messages=messages) as handle:
                        try:
                            completion = meter.complete(
                                entity=self.entity,
                                project=self.project,
                                model=spec.model_id,
                                messages=messages,
                                response_schema=schema,
                                max_tokens=spec.max_output_tokens,
                                seed=RANDOM_SEED,
                            )
                        except _PostReceiptResourceViolation as exc:
                            # A local resource guard is an operational outcome,
                            # not evidence that the provider failed to answer.
                            # Preserve and trace the received completion so the
                            # scientific endpoint remains observable while the
                            # end-to-end system outcome still fails closed.
                            completion = exc.completion
                            post_receipt_resource_violation = exc.as_dict()
                        if completion.model != spec.model_id:
                            raise RuntimeError("R4_PROVIDER_MODEL_MISMATCH")
                        if handle is not None:
                            handle.record(completion)
                    parsed, protocol_errors = _parse_response(completion.content, schema=schema)
                    validation = _validate_output(
                        stage=spec.stage,
                        payload=payload,
                        parsed=parsed,
                        protocol_errors=protocol_errors,
                    )
                    if trace is not None and hasattr(trace, "validation"):
                        trace.validation(
                            valid=bool(validation["provisional_model_conformance"]),
                            checked_claim_count=len(validation["expected_evidence_ids"]),
                            error_codes=validation["errors"],
                        )
                    if trace is not None and hasattr(trace, "assert_operations"):
                        trace.assert_operations(llm_calls=1, tool_calls=1, final_answer=False)
            except Exception as exc:  # noqa: BLE001 - preserve provisional result
                trace_fault = _safe_error_code(exc)
        except Exception as exc:  # noqa: BLE001 - research faults fail closed
            preparation_fault = _safe_error_code(exc)

        weave_receipt: Mapping[str, Any] = {}
        if trace is not None:
            try:
                weave_receipt = _weave_turn_receipt_projection(trace) or {}
            except Exception as exc:  # noqa: BLE001 - trace result is independent
                trace_fault = trace_fault or _safe_error_code(exc)
        trace_acknowledged = bool(weave_receipt) or self.execution_mode == "fake"
        if completion is not None and validation is None:
            # A tracing fault can happen after the provider responded but before
            # the in-context validation ran. Reconstruct it from the immutable
            # local response and request rather than discarding the model result.
            try:
                messages, schema = _model_request(spec=spec, payload=payload)
                del messages
                parsed, protocol_errors = _parse_response(completion.content, schema=schema)
                validation = _validate_output(
                    stage=spec.stage,
                    payload=payload,
                    parsed=parsed,
                    protocol_errors=protocol_errors,
                )
            except Exception as exc:  # noqa: BLE001 - reconstructed validation fails closed
                preparation_fault = preparation_fault or _safe_error_code(exc)
        provider_response_received = completion is not None
        validation = validation or {
            "provider_response_received": provider_response_received,
            "protocol_valid": False,
            "decision_fidelity": False,
            "evidence_binding": False,
            "semantic_validation": False if spec.stage == PROSE_STAGE else None,
            "provisional_model_conformance": False,
            "expected_plan_id": None,
            "expected_evidence_ids": [],
            "selected_plan_id": None,
            "errors": [],
        }
        errors = list(validation["errors"])
        if preparation_fault:
            errors.append(preparation_fault)
        if trace_fault:
            errors.append(trace_fault)
        if post_receipt_resource_violation:
            errors.append(str(post_receipt_resource_violation["error_code"]))
        if not trace_acknowledged:
            errors.append("trace_acknowledgement_missing")
        errors = sorted(set(errors))
        end_to_end = bool(
            validation["provisional_model_conformance"]
            and trace_acknowledged
            and not trace_fault
            and not preparation_fault
            and post_receipt_resource_violation is None
        )
        raw_content = completion.content if completion else ""
        details = {
            "study_version": STUDY_VERSION,
            "production_eligible": False,
            "answer_released": False,
            "provider_response_received": provider_response_received,
            "protocol_valid": bool(validation["protocol_valid"]),
            "decision_fidelity": bool(validation["decision_fidelity"]),
            "evidence_binding": bool(validation["evidence_binding"]),
            "semantic_validation": validation["semantic_validation"],
            "provisional_model_conformance": bool(validation["provisional_model_conformance"]),
            "trace_acknowledged": trace_acknowledged,
            "end_to_end_conformance": end_to_end,
            "selected_plan_id": validation["selected_plan_id"],
            "expected_evidence_count": len(validation["expected_evidence_ids"]),
            "parsed_response": parsed,
            "finish_reason": completion.finish_reason if completion else None,
            "raw_response_sha256": (
                hashlib.sha256(raw_content.encode()).hexdigest() if completion else None
            ),
            "raw_response_utf8_bytes": len(raw_content.encode()),
            "experimental_receipt": receipt,
            "experimental_receipt_valid": bool(receipt and validate_experimental_receipt(receipt)),
            "weave_turn_receipt": dict(weave_receipt),
            "trace_fault_code": trace_fault,
            "resource_policy_valid": post_receipt_resource_violation is None,
            "post_receipt_resource_violation": post_receipt_resource_violation,
            "request_accounting": meter.request_accounting(),
            "cost_accounting": meter.cost_accounting(price),
            "unmeasured_metrics": [
                "causal",
                "intent_correct",
                "safety_intent_correct",
                "injection_safe",
                "practitioner_usefulness",
            ],
            "answer_release_blocked_by_research_protocol": True,
        }
        return AttemptOutcome(
            attempt_id=spec.attempt_id,
            status="completed" if provider_response_received else "failed_closed",
            provider_invocations=meter.logical_calls,
            transport_attempts=meter.transport_attempts,
            input_tokens=meter.input_tokens,
            output_tokens=meter.output_tokens,
            cost_usd=meter.cost(price),
            latency_ms=(time.perf_counter() - started) * 1_000,
            protocol_valid=bool(validation["protocol_valid"]),
            # Broad safety and injection resistance are not measured here.
            safe=False,
            evidence_valid=bool(validation["evidence_binding"]),
            repository_fidelity=bool(
                validation["decision_fidelity"]
                and validation["evidence_binding"]
                and validation["semantic_validation"] is not False
            ),
            injection_safe=False,
            causal=None,
            intent_correct=None,
            safety_intent_correct=None,
            false_refusal=None,
            trace_acknowledged=trace_acknowledged,
            input_token_envelope=meter.input_token_envelope,
            output_token_envelope=meter.output_token_envelope,
            explanation_plan_id=(
                str(validation["selected_plan_id"]) if validation["selected_plan_id"] else None
            ),
            error_codes=tuple(errors),
            details=details,
        )


class PerfectGroundingFakeProvider:
    """Deterministic zero-network machinery control, never scientific evidence."""

    def complete(self, **kwargs: Any) -> InferenceCompletion:
        messages = kwargs["messages"]
        payload = json.loads(str(messages[-1]["content"]))
        schema = kwargs["response_schema"]
        expected = _expected_output(payload)
        value: dict[str, Any] = {
            "plan_id": expected["plan_id"],
            "evidence_ids": expected["evidence_ids"],
        }
        if "answer_text" in schema["properties"]:
            value.update(
                {
                    "response_mode": expected["response_mode"],
                    "answer_text": (
                        "The requested synthetic record supports the rule result."
                        if expected["evidence_ids"]
                        else "The requested synthetic record is unavailable."
                    ),
                    "limitation_ids": expected["required_limitation_ids"],
                    "action_ids": expected["required_action_ids"],
                }
            )
        content = json.dumps(value, sort_keys=True, separators=(",", ":"))
        request = build_inference_request_payload(
            model=str(kwargs["model"]),
            messages=messages,
            response_schema=schema,
            max_tokens=int(kwargs["max_tokens"]),
            seed=int(kwargs.get("seed", RANDOM_SEED)),
        )
        return InferenceCompletion(
            provider_call_id="r4c-fake-" + hashlib.sha256(request).hexdigest()[:16],
            model=str(kwargs["model"]),
            content=content,
            usage=InferenceUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            finish_reason="stop",
            transport_attempts=1,
            request_bytes=len(request),
            response_bytes=len(content.encode()),
            latency_ms=1.0,
        )


def _receipt_intervention_valid(receipt: Mapping[str, Any]) -> bool:
    if not validate_experimental_receipt(receipt):
        return False
    intervention = receipt.get("intervention")
    if not isinstance(intervention, Mapping):
        return False
    condition = str(intervention.get("condition") or "")
    original_target = intervention.get("original_target_record")
    current_target = intervention.get("current_target_record")
    original_distractor = intervention.get("original_distractor_record")
    current_distractor = intervention.get("current_distractor_record")
    if not isinstance(original_target, Mapping) or not isinstance(original_distractor, Mapping):
        return False

    def stable_except_value_and_record_hash(
        left: Mapping[str, Any], right: Mapping[str, Any]
    ) -> bool:
        def normalized(value: Mapping[str, Any]) -> dict[str, Any]:
            row = deepcopy(dict(value))
            row.pop("value", None)
            source = row.get("source")
            if isinstance(source, dict):
                source.pop("record_sha256", None)
            return row

        return normalized(left) == normalized(right)

    try:
        original_value = _finite_number(
            original_target.get("value"), code="R4_RECEIPT_VALUE_NONFINITE"
        )
        threshold = _finite_number(receipt.get("threshold"), code="R4_RECEIPT_THRESHOLD_NONFINITE")
        operator = str(receipt.get("operator") or "")
        if condition == "original_target":
            return bool(
                current_target == original_target and current_distractor == original_distractor
            )
        if condition == "target_threshold_crossing":
            if not isinstance(current_target, Mapping):
                return False
            current_value = _finite_number(
                current_target.get("value"), code="R4_RECEIPT_VALUE_NONFINITE"
            )
            return bool(
                current_distractor == original_distractor
                and stable_except_value_and_record_hash(original_target, current_target)
                and original_target.get("source", {}).get("record_sha256")
                != current_target.get("source", {}).get("record_sha256")
                and _rule_result(original_value, operator, threshold)
                != _rule_result(current_value, operator, threshold)
            )
        if condition == "target_withheld":
            return bool(
                current_target is None
                and current_distractor == original_distractor
                and intervention.get("target_alias")
                not in intervention.get("current_evidence_ids", ())
            )
        if condition == "distractor_only_perturbation":
            return bool(
                current_target == original_target
                and isinstance(current_distractor, Mapping)
                and stable_except_value_and_record_hash(original_distractor, current_distractor)
                and original_distractor.get("value") != current_distractor.get("value")
                and original_distractor.get("source", {}).get("record_sha256")
                != current_distractor.get("source", {}).get("record_sha256")
            )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False
    return False


def _exact_mcnemar_p(discordant_a_only: int, discordant_b_only: int) -> float:
    discordant = discordant_a_only + discordant_b_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, value)
        for value in range(min(discordant_a_only, discordant_b_only) + 1)
    ) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _row_metric(row: Mapping[str, Any], key: str) -> bool:
    details = row.get("details")
    return bool(isinstance(details, Mapping) and details.get(key) is True)


def _case_level_summary(
    rows: Sequence[Mapping[str, Any]], *, metric: str
) -> tuple[int, int, float]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["case_id"])].append(row)
    if any(len(values) != PRESENTATIONS for values in grouped.values()):
        raise RuntimeError("R4_PRESENTATION_PAIR_INCOMPLETE")
    successes = sum(all(_row_metric(row, metric) for row in values) for values in grouped.values())
    count = len(grouped)
    return successes, count, successes / count if count else 0.0


def analyze(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_schedule: Sequence[GroundingAttemptSpec],
) -> dict[str, Any]:
    """Analyze only an exact, schedule-bound 672-cell execution.

    Primary denominators are unique scenarios.  Both alias/order presentations
    must pass for a scenario to pass; presentation rows are never treated as
    independent observations and no Wilson intervals are reported.
    """

    schedule = tuple(expected_schedule)
    if len(schedule) != 672:
        raise RuntimeError("R4_EXPECTED_SCHEDULE_INVALID")
    expected = {spec.attempt_id: spec for spec in schedule}
    if len(expected) != 672 or len(rows) != 672:
        raise RuntimeError("R4_ANALYSIS_INCOMPLETE")
    observed = {str(row.get("attempt_id") or ""): row for row in rows}
    if len(observed) != 672 or set(observed) != set(expected):
        raise RuntimeError("R4_ANALYSIS_SCHEDULE_IDENTITY_MISMATCH")
    schedule_hash = json_sha256(schedule_records(schedule))
    instance_ids = {spec.study_instance_sha256 for spec in schedule}
    if len(instance_ids) != 1:
        raise RuntimeError("R4_ANALYSIS_INSTANCE_MISMATCH")
    for attempt_id, spec in expected.items():
        row = observed[attempt_id]
        for field in (
            "stage",
            "split",
            "case_id",
            "project_family",
            "model_id",
            "architecture_id",
            "prompt_id",
            "max_output_tokens",
            "repeat",
            "presentation_index",
            "ablation_condition",
            "time_block",
        ):
            if row.get(field) != getattr(spec, field):
                raise RuntimeError("R4_ANALYSIS_SCHEDULE_CELL_DRIFT")
        details = row.get("details")
        receipt = details.get("experimental_receipt") if isinstance(details, Mapping) else None
        if (
            not isinstance(receipt, Mapping)
            or not _receipt_intervention_valid(receipt)
            or receipt.get("attempt_id") != attempt_id
            or receipt.get("schedule_sha256") != schedule_hash
            or receipt.get("study_instance_sha256") != spec.study_instance_sha256
            or receipt.get("schedule_cell_sha256") != json_sha256(asdict(spec))
        ):
            raise RuntimeError("R4_EXPERIMENTAL_RECEIPT_INVALID")
        if bool(details.get("answer_released")):
            raise RuntimeError("R4_RESEARCH_ANSWER_RELEASED")

    selected = [observed[spec.attempt_id] for spec in schedule]
    # The 128/256 prose cells must be identical in every model-visible respect.
    cap_pairs: dict[tuple[str, str, str, str, int], dict[int, Mapping[str, Any]]] = defaultdict(
        dict
    )
    for row in selected:
        if row["stage"] != PROSE_STAGE:
            continue
        key = (
            str(row["split"]),
            str(row["case_id"]),
            str(row["model_id"]),
            str(row["ablation_condition"]),
            int(row["presentation_index"]),
        )
        cap_pairs[key][int(row["max_output_tokens"])] = row
    if len(cap_pairs) != 144 or any(
        set(pair) != set(PROSE_OUTPUT_CAPS) for pair in cap_pairs.values()
    ):
        raise RuntimeError("R4_PROSE_CAP_PAIR_INCOMPLETE")
    for pair in cap_pairs.values():
        receipts = [pair[cap]["details"]["experimental_receipt"] for cap in PROSE_OUTPUT_CAPS]
        if len({item["model_visible_contract_sha256"] for item in receipts}) != 1:
            raise RuntimeError("R4_PROSE_CAP_PROMPT_DRIFT")
        normalized = []
        for item in receipts:
            request = deepcopy(item["normalized_request"])
            request.pop("max_output_tokens", None)
            normalized.append(request)
        if normalized[0] != normalized[1]:
            raise RuntimeError("R4_PROSE_CAP_REQUEST_DRIFT")

    grounding_primary: list[dict[str, Any]] = []
    for split in CORPUS_SPLITS:
        for condition in GROUNDING_CONDITIONS:
            for model in MODELS:
                subset = [
                    row
                    for row in selected
                    if row["stage"] == GROUNDING_STAGE
                    and row["split"] == split
                    and row["ablation_condition"] == condition
                    and row["model_id"] == model
                ]
                record: dict[str, Any] = {
                    "split": split,
                    "project_families": list(CORPUS_SPLITS[split]),
                    "condition": condition,
                    "model_id": model,
                    "unique_case_count": 12,
                    "presentation_count_per_case": PRESENTATIONS,
                }
                for metric in (
                    "provider_response_received",
                    "protocol_valid",
                    "decision_fidelity",
                    "evidence_binding",
                    "trace_acknowledged",
                    "end_to_end_conformance",
                ):
                    success, count, rate = _case_level_summary(subset, metric=metric)
                    if count != 12:
                        raise RuntimeError("R4_GROUNDING_PRIMARY_CASE_COUNT_INVALID")
                    record[f"{metric}_case_success"] = success
                    record[f"{metric}_case_rate"] = rate
                grounding_primary.append(record)

    mechanism_rows: list[dict[str, Any]] = []
    for split in CORPUS_SPLITS:
        for model in MODELS:
            subset = [
                row
                for row in selected
                if row["stage"] == GROUNDING_STAGE
                and row["split"] == split
                and row["model_id"] == model
            ]
            by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in subset:
                by_case[str(row["case_id"])].append(row)
            sensitivity = specificity = abstention = complete = 0
            for case_rows in by_case.values():
                by_cell = {
                    (int(row["presentation_index"]), str(row["ablation_condition"])): row
                    for row in case_rows
                }
                if len(by_cell) != PRESENTATIONS * len(GROUNDING_CONDITIONS):
                    raise RuntimeError("R4_GROUNDING_MECHANISM_CELL_INCOMPLETE")
                case_sensitivity = case_specificity = case_abstention = True
                for presentation in range(PRESENTATIONS):
                    original = by_cell[(presentation, "original_target")]
                    crossing = by_cell[(presentation, "target_threshold_crossing")]
                    withheld = by_cell[(presentation, "target_withheld")]
                    distractor = by_cell[(presentation, "distractor_only_perturbation")]
                    case_sensitivity &= bool(
                        _row_metric(original, "decision_fidelity")
                        and _row_metric(crossing, "decision_fidelity")
                        and original["details"].get("selected_plan_id")
                        != crossing["details"].get("selected_plan_id")
                    )
                    case_specificity &= bool(
                        _row_metric(original, "decision_fidelity")
                        and _row_metric(distractor, "decision_fidelity")
                        and original["details"].get("selected_plan_id")
                        == distractor["details"].get("selected_plan_id")
                    )
                    withheld_response = withheld["details"].get("parsed_response")
                    case_abstention &= bool(
                        _row_metric(withheld, "decision_fidelity")
                        and _row_metric(withheld, "evidence_binding")
                        and isinstance(withheld_response, Mapping)
                        and withheld_response.get("evidence_ids") == []
                    )
                sensitivity += case_sensitivity
                specificity += case_specificity
                abstention += case_abstention
                complete += case_sensitivity and case_specificity and case_abstention
            count = len(by_case)
            if count != 12:
                raise RuntimeError("R4_GROUNDING_MECHANISM_CASE_COUNT_INVALID")
            mechanism_rows.append(
                {
                    "split": split,
                    "project_families": list(CORPUS_SPLITS[split]),
                    "model_id": model,
                    "unique_case_count": count,
                    "threshold_sensitivity_case_success": sensitivity,
                    "distractor_specificity_case_success": specificity,
                    "missing_evidence_abstention_case_success": abstention,
                    "complete_mechanism_case_success": complete,
                    "complete_mechanism_case_rate": complete / count,
                }
            )

    prose_primary: list[dict[str, Any]] = []
    mcnemar_rows: list[dict[str, Any]] = []
    for split in CORPUS_SPLITS:
        for condition in PROSE_CONDITIONS:
            for model in MODELS:
                by_cap_case: dict[int, dict[str, bool]] = {}
                for cap in PROSE_OUTPUT_CAPS:
                    subset = [
                        row
                        for row in selected
                        if row["stage"] == PROSE_STAGE
                        and row["split"] == split
                        and row["ablation_condition"] == condition
                        and row["model_id"] == model
                        and int(row["max_output_tokens"]) == cap
                    ]
                    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
                    for row in subset:
                        grouped[str(row["case_id"])].append(row)
                    if len(grouped) != 6 or any(
                        len(values) != PRESENTATIONS for values in grouped.values()
                    ):
                        raise RuntimeError("R4_PROSE_PRIMARY_CASE_COUNT_INVALID")
                    cap_success = {
                        case_id: all(bool(row["protocol_valid"]) for row in values)
                        for case_id, values in grouped.items()
                    }
                    by_cap_case[cap] = cap_success
                    successes = sum(cap_success.values())
                    prose_primary.append(
                        {
                            "split": split,
                            "project_families": list(CORPUS_SPLITS[split]),
                            "condition": condition,
                            "model_id": model,
                            "max_output_tokens": cap,
                            "unique_case_count": len(grouped),
                            "presentation_count_per_case": PRESENTATIONS,
                            "protocol_valid_case_success": successes,
                            "protocol_valid_case_rate": successes / len(grouped),
                        }
                    )
                case_ids = set(by_cap_case[128])
                if case_ids != set(by_cap_case[256]):
                    raise RuntimeError("R4_PROSE_CAP_CASE_PAIR_MISMATCH")
                success_128_only = sum(
                    by_cap_case[128][case_id] and not by_cap_case[256][case_id]
                    for case_id in case_ids
                )
                success_256_only = sum(
                    by_cap_case[256][case_id] and not by_cap_case[128][case_id]
                    for case_id in case_ids
                )
                mcnemar_rows.append(
                    {
                        "split": split,
                        "project_families": list(CORPUS_SPLITS[split]),
                        "condition": condition,
                        "model_id": model,
                        "paired_unique_case_count": len(case_ids),
                        "success_128_only": success_128_only,
                        "success_256_only": success_256_only,
                        "discordant_pairs": success_128_only + success_256_only,
                        "exact_two_sided_mcnemar_p": _exact_mcnemar_p(
                            success_128_only, success_256_only
                        ),
                    }
                )

    diagnostics: list[dict[str, Any]] = []
    for stage in STAGES:
        for model in MODELS:
            subset = [row for row in selected if row["stage"] == stage and row["model_id"] == model]
            diagnostics.append(
                {
                    "stage": stage,
                    "model_id": model,
                    "attempt_count": len(subset),
                    "provider_responses": sum(
                        _row_metric(row, "provider_response_received") for row in subset
                    ),
                    "protocol_valid_attempts": sum(bool(row["protocol_valid"]) for row in subset),
                    "trace_acknowledged_attempts": sum(
                        bool(row["trace_acknowledged"]) for row in subset
                    ),
                    "end_to_end_conformant_attempts": sum(
                        _row_metric(row, "end_to_end_conformance") for row in subset
                    ),
                    "input_tokens": sum(int(row["input_tokens"]) for row in subset),
                    "output_tokens": sum(int(row["output_tokens"]) for row in subset),
                    "projected_cost_usd": str(
                        sum((Decimal(str(row["cost_usd"])) for row in subset), Decimal(0))
                    ),
                    "error_codes": dict(
                        sorted(
                            Counter(
                                str(code) for row in subset for code in row.get("error_codes", ())
                            ).items()
                        )
                    ),
                }
            )
    body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": next(iter(instance_ids)),
        "schedule_sha256": schedule_hash,
        "attempt_count": len(selected),
        "stage_attempt_counts": dict(sorted(Counter(row["stage"] for row in selected).items())),
        "all_experimental_receipts_valid": True,
        "d1_positive_control_gate_sha256": selected[0]["details"]["experimental_receipt"][
            "d1_positive_control_gate_sha256"
        ],
        "grounding_primary_unique_case_results": grounding_primary,
        "grounding_mechanism_results": mechanism_rows,
        "prose_cap_primary_unique_case_results": prose_primary,
        "prose_cap_paired_mcnemar": mcnemar_rows,
        "attempt_level_diagnostics_not_inferential_units": diagnostics,
        "statistical_unit": (
            "Unique scenario. Both alias/order presentations must pass; presentations are "
            "repeated measurements and are not counted as independent trials."
        ),
        "interval_policy": "No pseudoreplicated Wilson intervals are reported.",
        "actual_answers_released": 0,
        "production_release_eligible": False,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
        "claim_boundary": (
            "This study measures synthetic evidence sensitivity, target binding, rule fidelity, "
            "structured-response reliability, and trace acknowledgement. It does not measure "
            "hydraulic accuracy, ecological benefit, field performance, practitioner usefulness, "
            "prompt-injection resistance, intent classification, or production safety."
        ),
    }
    return {**body, "analysis_sha256": json_sha256(body)}


def build_preregistration(
    corpus: Corpus,
    *,
    pricing: Mapping[str, ModelPrice],
    source_hashes: Mapping[str, str],
    prior_usage: Mapping[str, Any],
    execution_mode: str,
    caps: BudgetCaps,
) -> tuple[dict[str, Any], tuple[GroundingAttemptSpec, ...]]:
    if execution_mode not in {"fake", "live"}:
        raise ValueError("R4 execution mode is invalid")
    if any(model not in pricing for model in MODELS):
        raise ValueError("R4 pricing snapshot is incomplete")
    r4b_amendment_sha256 = str(source_hashes.get("r4b_protocol_amendment") or "")
    r4c_amendment_sha256 = str(source_hashes.get("r4c_protocol_amendment") or "")
    r4b_snapshot_sha256 = str(source_hashes.get("r4b_incident_snapshot") or "")
    if not all(
        _SHA256.fullmatch(value)
        for value in (
            r4b_amendment_sha256,
            r4c_amendment_sha256,
            r4b_snapshot_sha256,
        )
    ):
        raise ValueError("R4C_PROTOCOL_AMENDMENT_SOURCES_UNAVAILABLE")
    d1_gate = d1_positive_control(corpus)
    instance = json_sha256(
        {
            "study_version": STUDY_VERSION,
            "execution_mode": execution_mode,
            "corpus_manifest_sha256": corpus.manifest_sha256,
            "split_sha256": dict(sorted(corpus.split_sha256.items())),
            "models": list(MODELS),
            "grounding_conditions": list(GROUNDING_CONDITIONS),
            "prose_conditions": list(PROSE_CONDITIONS),
            "presentations": PRESENTATIONS,
            "prose_output_caps": list(PROSE_OUTPUT_CAPS),
            "prose_scenario_ids": list(PROSE_SCENARIO_IDS),
            "seed": RANDOM_SEED,
            "source_hashes": dict(sorted(source_hashes.items())),
            "d1_positive_control_gate_sha256": d1_gate["gate_sha256"],
        }
    )
    specs = build_schedule(corpus, instance_sha256=instance)
    schedule = schedule_records(specs)
    schedule_hash = json_sha256(schedule)
    estimate = estimate_provider_budget(
        specs,
        pricing=pricing,
        maximum_input_tokens_per_call=R4C_MAX_INPUT_TOKENS,
    )
    combined = {
        "cost_usd": str(
            Decimal(str(prior_usage["cost_usd"])) + Decimal(str(estimate["worst_case_cost_usd"]))
        ),
        "provider_invocations": int(prior_usage["provider_invocations"])
        + int(estimate["logical_provider_invocations"]),
        "transport_attempts": int(prior_usage["transport_attempts"])
        + int(estimate["worst_case_transport_attempts"]),
        "input_tokens": int(prior_usage["input_tokens"]) + int(estimate["worst_case_input_tokens"]),
        "output_tokens": int(prior_usage["output_tokens"])
        + int(estimate["worst_case_output_tokens"]),
    }
    if (
        Decimal(combined["cost_usd"]) > caps.cost_usd
        or combined["provider_invocations"] > caps.provider_invocations
        or combined["transport_attempts"] > caps.provider_invocations
        or combined["input_tokens"] > caps.input_tokens
        or combined["output_tokens"] > caps.output_tokens
    ):
        raise RuntimeError("R4_COMBINED_BUDGET_EXCEEDS_CAP")
    case_maps = {split: corpus.case_map(split) for split in CORPUS_SPLITS}
    request_sizes: list[int] = []
    model_visible_records: list[dict[str, Any]] = []
    prose_contracts: dict[tuple[str, str, str, str, int], dict[int, str]] = defaultdict(dict)
    for spec in specs:
        case = case_maps[spec.split][spec.case_id]
        payload, _projection = _project_payload(
            case,
            condition=str(spec.ablation_condition),
            presentation=spec.presentation_index,
        )
        messages, schema = _model_request(spec=spec, payload=payload)
        request = build_inference_request_payload(
            model=spec.model_id,
            messages=messages,
            response_schema=schema,
            max_tokens=spec.max_output_tokens,
            seed=RANDOM_SEED,
        )
        request_sizes.append(len(request))
        visible_contract = json_sha256(
            {
                "messages": messages,
                "response_schema": schema,
                "seed": RANDOM_SEED,
                "temperature": 0,
            }
        )
        model_visible_records.append(
            {
                "attempt_id": spec.attempt_id,
                "model_visible_contract_sha256": visible_contract,
                "request_utf8_bytes": len(request),
            }
        )
        if spec.stage == PROSE_STAGE:
            key = (
                spec.split,
                spec.case_id,
                spec.model_id,
                str(spec.ablation_condition),
                spec.presentation_index,
            )
            prose_contracts[key][spec.max_output_tokens] = visible_contract
    if (
        len(prose_contracts) != 144
        or any(set(pair) != set(PROSE_OUTPUT_CAPS) for pair in prose_contracts.values())
        or any(len(set(pair.values())) != 1 for pair in prose_contracts.values())
    ):
        raise RuntimeError("R4_PROSE_CAP_CONTRACT_NOT_IDENTICAL")
    if max(request_sizes) != 8017 or max(request_sizes) >= R4C_MAX_INPUT_TOKENS:
        raise RuntimeError("R4C_NORMALIZED_REQUEST_ENVELOPE_DRIFT")
    body = {
        "schema_version": "1.0",
        "preregistration_version": PREREGISTRATION_VERSION,
        "study_version": STUDY_VERSION,
        "study_instance_sha256": instance,
        "execution_mode": execution_mode,
        "research_question": (
            "Can a model bind the requested synthetic repository record, apply one explicit "
            "screening rule, ignore a perturbed distractor, and abstain when the target is absent; "
            "and does a 128-token versus 256-token cap change full-prose JSON parse success?"
        ),
        "design": {
            "provider_call_count": 672,
            "grounding": {
                "cases": 24,
                "conditions": list(GROUNDING_CONDITIONS),
                "models": list(MODELS),
                "alias_order_presentations": PRESENTATIONS,
                "provider_calls": 384,
                "response_schema": GROUNDING_SCHEMA_ID,
            },
            "prose_diagnosis": {
                "deterministically_stratified_cases": 12,
                "scenario_ids": list(PROSE_SCENARIO_IDS),
                "conditions": list(PROSE_CONDITIONS),
                "models": list(MODELS),
                "alias_order_presentations": PRESENTATIONS,
                "output_token_caps": list(PROSE_OUTPUT_CAPS),
                "provider_calls": 288,
                "response_schema": PROSE_SCHEMA_ID,
                "prompt_and_schema_identical_within_cap_pair": True,
            },
            "project_family_pairs": {
                split: list(families) for split, families in CORPUS_SPLITS.items()
            },
            "condition_hidden_from_model": True,
            "distractors_retained_shuffled_and_remapped": True,
            "temperature": 0,
            "seed": RANDOM_SEED,
            "time_blocks": list(TIME_BLOCKS),
            "conditions_rotated_across_time_blocks": True,
            "paired_prose_caps_share_time_block": True,
            "concurrency": 2,
            "transport_policy": {
                "actual_max_transport_attempts_per_cell": 1,
                "runtime_retry_enabled": False,
                "reservation_policy": (
                    "The shared campaign ledger retains its conservative legacy two-transport "
                    "envelope. That 1,344-transport reservation is a budget bound, not a planned "
                    "or actual retry count; R4 configures one transport attempt per 672 cells."
                ),
            },
        },
        "d1_positive_control": d1_gate,
        "primary_endpoints": [
            "grounding_decision_fidelity_by_unique_case",
            "grounding_evidence_binding_by_unique_case",
            "threshold_sensitivity_by_unique_case",
            "distractor_specificity_by_unique_case",
            "missing_evidence_abstention_by_unique_case",
            "paired_prose_protocol_parse_success_128_vs_256",
        ],
        "outcome_decomposition": [
            "provider_response_received",
            "protocol_valid",
            "decision_fidelity",
            "evidence_binding",
            "semantic_validation",
            "trace_acknowledged",
            "resource_policy_valid",
            "end_to_end_conformance",
        ],
        "analysis_policy": {
            "schedule_bound": True,
            "primary_unit": "unique_scenario",
            "presentations_are_repeated_measurements": True,
            "case_pass_requires_both_presentations": True,
            "stratify_by": ["project_family_pair", "condition", "model"],
            "prose_cap_test": "paired_exact_McNemar_on_unique_cases",
            "pseudoreplicated_Wilson_intervals": False,
            "dirty_or_missing_receipt_blocks_analysis": True,
            "all_failures_retained": True,
            "no_model_answer_released": True,
        },
        "unmeasured_attempt_outcomes": [
            "causal",
            "intent_correct",
            "safety_intent_correct",
            "injection_safe",
            "practitioner_usefulness",
        ],
        "source_boundary": {
            "provider_receives": "projected provider_payload only",
            "provider_never_receives": [
                "evaluator_only",
                "condition_id",
                "expected plan",
                "original opaque identifiers",
                "local paths",
            ],
        },
        "protocol_amendment": {
            "amendment_id": "gi-evidence-guide-r4c-input-cap-amendment-2026-08-07",
            "source_capsule_logical_name": "r4c_protocol_amendment",
            "source_sha256": r4c_amendment_sha256,
            "predecessor_amendment_sha256": r4b_amendment_sha256,
            "incident_snapshot_source_sha256": r4b_snapshot_sha256,
            "r4b_attempts_reused": 0,
            "r4b_responses_reused": 0,
            "clean_start_required": True,
        },
        "release_policy": {
            "release_eligible": False,
            "production_state": "DISABLE_LIVE_GUIDE",
            "practitioner_usefulness_status": "not_measured",
        },
        "corpus": {
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
            "manifest_sha256": corpus.manifest_sha256,
            "split_sha256": {split: corpus.split_sha256[split] for split in CORPUS_SPLITS},
        },
        "request_audit": {
            "payload_count": len(request_sizes),
            "minimum_utf8_bytes": min(request_sizes),
            "maximum_utf8_bytes": max(request_sizes),
            "transport_byte_cap": 64 * 1024,
            "provider_input_token_cap": R4C_MAX_INPUT_TOKENS,
            "minimum_byte_headroom_under_provider_input_cap": (
                R4C_MAX_INPUT_TOKENS - max(request_sizes)
            ),
            "cap_rationale": (
                "The maximum normalized request is 8,017 UTF-8 bytes. R4c reserves "
                "8,192 provider input tokens per transport, conservatively above the full "
                "request byte length. Bytes remain a transport diagnostic; provider-reported "
                "usage remains the measured token count."
            ),
            "request_bytes_used_as_measured_token_count": False,
            "model_visible_records_sha256": json_sha256(model_visible_records),
        },
        "schedule_sha256": schedule_hash,
        "schedule_records": schedule,
        "budget": {
            "prior_cumulative_usage": dict(prior_usage),
            "incremental_worst_case": estimate,
            "combined_worst_case": combined,
            "caps": {
                "cost_usd": str(caps.cost_usd),
                "provider_invocations": caps.provider_invocations,
                "input_tokens": caps.input_tokens,
                "output_tokens": caps.output_tokens,
            },
        },
        "source_hashes": dict(sorted(source_hashes.items())),
        "claim_boundary": (
            "Exploratory automated conformance only. No hydraulic validity, ecological outcome, "
            "field performance, practitioner usefulness, prompt-injection resistance, intent "
            "classification, production safety, or production release is measured."
        ),
    }
    return {**body, "preregistration_sha256": json_sha256(body)}, specs


def source_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return {name: file_sha256(path) for name, path in sorted(paths.items())}


__all__ = [
    "CONDITIONS",
    "CORPUS_SPLITS",
    "GROUNDING_CONDITIONS",
    "GROUNDING_STAGE",
    "MAX_OUTPUT_TOKENS",
    "MODELS",
    "OUTPUT_DIRECTORY_NAME",
    "PROSE_CONDITIONS",
    "PROSE_OUTPUT_CAPS",
    "PROSE_SCENARIO_IDS",
    "PROSE_STAGE",
    "R4C_MAX_INPUT_TOKENS",
    "STAGE",
    "STAGES",
    "STUDY_VERSION",
    "GroundingAttemptSpec",
    "GroundingExecutor",
    "PerfectGroundingFakeProvider",
    "analyze",
    "build_preregistration",
    "build_schedule",
    "d1_positive_control",
    "load_r4_corpus",
    "schedule_records",
    "source_hashes",
    "validate_experimental_receipt",
    "validate_semantic_receipt",
]
