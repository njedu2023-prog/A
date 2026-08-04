from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .domain import ContractError, normalize_date
from .ranking_engine import MODEL_ID


REGISTRY_SCHEMA = "model_registry_v1"
BASELINE_MODEL_ID = MODEL_ID
LEGACY_BASELINE_MODEL_IDS = frozenset({"transparent_shadow_baseline_v1"})
CHALLENGER_STATUSES = frozenset(
    {"INSUFFICIENT_DATA", "CANDIDATE", "REJECTED", "PROMOTED"}
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _is_mature(row: Mapping[str, Any]) -> bool:
    labels = row.get("labels")
    return isinstance(labels, Mapping) and labels.get("is_mature") is True


def evaluate_promotion_readiness(
    rows: Iterable[Mapping[str, Any]],
    *,
    minimum_mature_candidates: int = 180,
    minimum_per_fixed_rank: int = 60,
    minimum_lockbox_days: int = 126,
) -> dict[str, Any]:
    for name, value in (
        ("minimum_mature_candidates", minimum_mature_candidates),
        ("minimum_per_fixed_rank", minimum_per_fixed_rank),
        ("minimum_lockbox_days", minimum_lockbox_days),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ContractError(f"{name} must be a positive integer")

    mature = [item for item in rows if _is_mature(item)]
    rank_counts = Counter(
        int(item["rank"])
        for item in mature
        if isinstance(item.get("rank"), int) and not isinstance(item.get("rank"), bool)
    )
    lockbox_dates = sorted(
        {normalize_date(item.get("decision_date"), "decision_date") for item in mature}
    )
    reasons: list[str] = []
    if len(mature) < minimum_mature_candidates:
        reasons.append("mature_candidates_below_180")
    for rank in (1, 2, 3):
        if rank_counts[rank] < minimum_per_fixed_rank:
            reasons.append(f"rank_{rank}_mature_candidates_below_60")
    if len(lockbox_dates) < minimum_lockbox_days:
        reasons.append("lockbox_decision_days_below_126")
    return {
        "status": "ELIGIBLE_FOR_VALIDATION" if not reasons else "INSUFFICIENT_DATA",
        "mature_candidates": len(mature),
        "fixed_rank_mature_counts": {
            str(rank): rank_counts[rank] for rank in (1, 2, 3)
        },
        "mature_decision_days": len(lockbox_dates),
        "required": {
            "mature_candidates": minimum_mature_candidates,
            "per_fixed_rank": minimum_per_fixed_rank,
            "lockbox_decision_days": minimum_lockbox_days,
        },
        "reasons": reasons,
    }


def baseline_champion() -> dict[str, Any]:
    return {
        "model_id": BASELINE_MODEL_ID,
        "kind": "BASELINE",
        "status": "ACTIVE",
        "artifact_path": None,
        "artifact_sha256": None,
    }


def create_registry(rows: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    readiness = evaluate_promotion_readiness(rows)
    return {
        "schema_version": REGISTRY_SCHEMA,
        "champion": baseline_champion(),
        "challenger": {
            "model_id": None,
            "status": (
                "CANDIDATE"
                if readiness["status"] == "ELIGIBLE_FOR_VALIDATION"
                else "INSUFFICIENT_DATA"
            ),
            "artifact_path": None,
            "artifact_sha256": None,
        },
        "promotion_readiness": readiness,
    }


def _validate_artifact_fields(model: Mapping[str, Any], *, required: bool) -> None:
    artifact_path = model.get("artifact_path")
    checksum = model.get("artifact_sha256")
    if required and (not artifact_path or not checksum):
        raise ContractError("trained model requires artifact_path and artifact_sha256")
    if artifact_path is not None:
        path = Path(str(artifact_path))
        if path.is_absolute() or ".." in path.parts:
            raise ContractError("model artifact_path must be a contained relative path")
    if checksum is not None and not SHA256_PATTERN.fullmatch(str(checksum)):
        raise ContractError("model artifact_sha256 must be lowercase SHA-256")


def validate_registry(registry: Mapping[str, Any]) -> None:
    if not isinstance(registry, Mapping) or registry.get("schema_version") != REGISTRY_SCHEMA:
        raise ContractError("unsupported model registry schema")
    champion = registry.get("champion")
    challenger = registry.get("challenger")
    readiness = registry.get("promotion_readiness")
    if not isinstance(champion, Mapping):
        raise ContractError("registry champion must be an object")
    if champion.get("kind") not in {"BASELINE", "TRAINED"}:
        raise ContractError("champion kind must be BASELINE or TRAINED")
    if champion.get("status") != "ACTIVE" or not champion.get("model_id"):
        raise ContractError("champion must be an active named model")
    if champion.get("kind") == "BASELINE":
        if champion.get("model_id") not in {
            BASELINE_MODEL_ID,
            *LEGACY_BASELINE_MODEL_IDS,
        }:
            raise ContractError("baseline champion id is not recognized")
        _validate_artifact_fields(champion, required=False)
        if champion.get("artifact_path") is not None or champion.get("artifact_sha256") is not None:
            raise ContractError("baseline champion cannot require an artifact")
    else:
        _validate_artifact_fields(champion, required=True)

    if not isinstance(challenger, Mapping):
        raise ContractError("registry challenger must be an object")
    status = challenger.get("status")
    if status not in CHALLENGER_STATUSES:
        raise ContractError("unsupported challenger status")
    requires_artifact = status in {"CANDIDATE", "PROMOTED"} and bool(
        challenger.get("model_id")
    )
    _validate_artifact_fields(challenger, required=requires_artifact)
    if not isinstance(readiness, Mapping) or readiness.get("status") not in {
        "INSUFFICIENT_DATA",
        "ELIGIBLE_FOR_VALIDATION",
    }:
        raise ContractError("promotion_readiness is invalid")


def artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_file(base_dir: str | Path, relative_path: Any) -> Path:
    path = Path(str(relative_path))
    if path.is_absolute() or ".." in path.parts:
        raise ContractError("model artifact_path must be a contained relative path")
    base = Path(base_dir).resolve()
    resolved = (base / path).resolve()
    if resolved != base and base not in resolved.parents:
        raise ContractError("model artifact escapes registry base directory")
    return resolved


def resolve_champion(
    registry: Mapping[str, Any],
    *,
    base_dir: str | Path = ".",
) -> dict[str, Any]:
    """Resolve the active model, failing safely to the transparent baseline."""

    validate_registry(registry)
    champion = dict(registry["champion"])
    if champion["kind"] == "BASELINE":
        return {
            "model": champion,
            "fallback": False,
            "fallback_reason": None,
        }
    try:
        artifact = _artifact_file(base_dir, champion["artifact_path"])
        observed = artifact_sha256(artifact)
    except (OSError, ContractError):
        return {
            "model": baseline_champion(),
            "fallback": True,
            "fallback_reason": "CHAMPION_ARTIFACT_UNAVAILABLE",
        }
    if observed != champion["artifact_sha256"]:
        return {
            "model": baseline_champion(),
            "fallback": True,
            "fallback_reason": "CHAMPION_ARTIFACT_CHECKSUM_MISMATCH",
        }
    return {
        "model": champion,
        "fallback": False,
        "fallback_reason": None,
    }
