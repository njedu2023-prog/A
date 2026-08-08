from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .domain import ContractError


PROMOTION_CERTIFICATE_SCHEMA = "promotion_certificate_v1"
PROMOTION_REPORT_SCHEMA = "model_promotion_report_v1"
PROMOTION_STATES = frozenset(
    {
        "INSUFFICIENT_DATA",
        "CANDIDATE",
        "EVALUATING",
        "REJECTED",
        "APPROVED",
        "PROMOTED",
    }
)
PROMOTION_TRANSITIONS = {
    "INSUFFICIENT_DATA": frozenset({"CANDIDATE"}),
    "CANDIDATE": frozenset({"EVALUATING", "REJECTED"}),
    "EVALUATING": frozenset({"APPROVED", "REJECTED"}),
    "REJECTED": frozenset({"CANDIDATE"}),
    "APPROVED": frozenset({"PROMOTED", "REJECTED"}),
    "PROMOTED": frozenset(),
}
REQUIRED_PROMOTION_CHECKS = (
    "sample_gate",
    "walk_forward",
    "calibration",
    "after_cost_return",
    "drawdown_and_cvar",
    "stress_tests",
    "lockbox",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_FINGERPRINT_EXCLUDES = frozenset(
    {"artifact_fingerprint", "promotion_certificate", "promotion_state"}
)


def _fingerprint(
    payload: Mapping[str, Any],
    *,
    exclude: frozenset[str] = frozenset(),
) -> str:
    if not isinstance(payload, Mapping):
        raise ContractError("fingerprint payload must be an object")
    normalized = {
        str(key): value for key, value in payload.items() if str(key) not in exclude
    }
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("fingerprint payload must be canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def artifact_fingerprint(artifact: Mapping[str, Any]) -> str:
    """Fingerprint the immutable learned payload, excluding governance wrappers."""

    return _fingerprint(artifact, exclude=_ARTIFACT_FINGERPRINT_EXCLUDES)


def promotion_report_fingerprint(report: Mapping[str, Any]) -> str:
    return _fingerprint(report, exclude=frozenset({"report_fingerprint"}))


def validate_promotion_transition(current: str, target: str) -> None:
    if current not in PROMOTION_STATES or target not in PROMOTION_STATES:
        raise ContractError("unsupported promotion state")
    if current == target:
        return
    if target not in PROMOTION_TRANSITIONS[current]:
        raise ContractError(f"illegal promotion transition: {current}->{target}")


def transition_promotion_state(
    record: Mapping[str, Any],
    target: str,
) -> dict[str, Any]:
    """Return an idempotently transitioned copy of a governance record."""

    current = str(record.get("promotion_state") or record.get("status") or "")
    validate_promotion_transition(current, target)
    result = copy.deepcopy(dict(record))
    result["promotion_state"] = target
    if "status" in result:
        result["status"] = target
    return result


def _require_sha256(value: Any, field: str) -> str:
    parsed = str(value or "")
    if not _SHA256.fullmatch(parsed):
        raise ContractError(f"{field} must be lowercase SHA-256")
    return parsed


def validate_approved_promotion_report(
    report: Mapping[str, Any],
    *,
    model_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    if not isinstance(report, Mapping) or report.get("schema") != PROMOTION_REPORT_SCHEMA:
        raise ContractError("unsupported promotion report schema")
    if report.get("status") != "APPROVED" or report.get(
        "promotion_state", "APPROVED"
    ) != "APPROVED":
        raise ContractError("promotion report is not approved")
    if str(report.get("model_id") or "") != model_id:
        raise ContractError("promotion report model_id mismatch")
    observed_artifact = _require_sha256(
        report.get("artifact_fingerprint"),
        "promotion report artifact_fingerprint",
    )
    if observed_artifact != expected_artifact_fingerprint:
        raise ContractError("promotion report artifact fingerprint mismatch")
    _require_sha256(
        report.get("evaluation_dataset_fingerprint"),
        "promotion report evaluation_dataset_fingerprint",
    )
    checks = report.get("checks")
    if not isinstance(checks, Mapping) or any(
        checks.get(name) is not True for name in REQUIRED_PROMOTION_CHECKS
    ):
        raise ContractError("promotion report required checks have not all passed")
    declared = report.get("report_fingerprint")
    if declared is not None and (
        _require_sha256(declared, "promotion report fingerprint")
        != promotion_report_fingerprint(report)
    ):
        raise ContractError("promotion report fingerprint mismatch")


def issue_promotion_certificate(
    artifact: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    model_id = str(artifact.get("model_id") or "")
    if not model_id:
        raise ContractError("learned artifact model_id is required")
    artifact_sha = artifact_fingerprint(artifact)
    validate_approved_promotion_report(
        report,
        model_id=model_id,
        expected_artifact_fingerprint=artifact_sha,
    )
    payload = {
        "schema": PROMOTION_CERTIFICATE_SCHEMA,
        "promotion_state": "APPROVED",
        "model_id": model_id,
        "artifact_fingerprint": artifact_sha,
        "promotion_report_fingerprint": promotion_report_fingerprint(report),
        "evaluation_dataset_fingerprint": str(
            report["evaluation_dataset_fingerprint"]
        ),
    }
    payload["certificate_fingerprint"] = _fingerprint(payload)
    return payload


def validate_promotion_certificate(
    certificate: Mapping[str, Any],
    *,
    model_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    if (
        not isinstance(certificate, Mapping)
        or certificate.get("schema") != PROMOTION_CERTIFICATE_SCHEMA
    ):
        raise ContractError("promotion certificate is missing or unsupported")
    if certificate.get("promotion_state") != "APPROVED":
        raise ContractError("promotion certificate is not approved")
    if str(certificate.get("model_id") or "") != model_id:
        raise ContractError("promotion certificate model_id mismatch")
    artifact_sha = _require_sha256(
        certificate.get("artifact_fingerprint"),
        "promotion certificate artifact_fingerprint",
    )
    if artifact_sha != expected_artifact_fingerprint:
        raise ContractError("promotion certificate artifact fingerprint mismatch")
    _require_sha256(
        certificate.get("promotion_report_fingerprint"),
        "promotion certificate report fingerprint",
    )
    _require_sha256(
        certificate.get("evaluation_dataset_fingerprint"),
        "promotion certificate evaluation dataset fingerprint",
    )
    declared = _require_sha256(
        certificate.get("certificate_fingerprint"),
        "promotion certificate fingerprint",
    )
    if declared != _fingerprint(
        certificate,
        exclude=frozenset({"certificate_fingerprint"}),
    ):
        raise ContractError("promotion certificate fingerprint mismatch")


def attach_promotion_certificate(
    artifact: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach an approved certificate without changing the model core fingerprint."""

    result = copy.deepcopy(dict(artifact))
    fingerprint = artifact_fingerprint(result)
    certificate = issue_promotion_certificate(result, report)
    result["artifact_fingerprint"] = fingerprint
    result["promotion_state"] = "APPROVED"
    result["promotion_certificate"] = certificate
    return result


def validate_certified_artifact(
    artifact: Mapping[str, Any],
    *,
    expected_certificate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(artifact, Mapping):
        raise ContractError("learned artifact must be an object")
    model_id = str(artifact.get("model_id") or "")
    if not model_id:
        raise ContractError("learned artifact model_id is required")
    certificate = artifact.get("promotion_certificate")
    if not isinstance(certificate, Mapping):
        raise ContractError("learned artifact requires a promotion certificate")
    observed = artifact_fingerprint(artifact)
    declared = _require_sha256(
        artifact.get("artifact_fingerprint"),
        "learned artifact fingerprint",
    )
    if observed != declared:
        raise ContractError("learned artifact fingerprint mismatch")
    validate_promotion_certificate(
        certificate,
        model_id=model_id,
        expected_artifact_fingerprint=observed,
    )
    if expected_certificate is not None and dict(expected_certificate) != dict(certificate):
        raise ContractError("registry and artifact promotion certificates disagree")
    return dict(certificate)


__all__ = [
    "PROMOTION_CERTIFICATE_SCHEMA",
    "PROMOTION_REPORT_SCHEMA",
    "PROMOTION_STATES",
    "REQUIRED_PROMOTION_CHECKS",
    "artifact_fingerprint",
    "attach_promotion_certificate",
    "issue_promotion_certificate",
    "promotion_report_fingerprint",
    "transition_promotion_state",
    "validate_approved_promotion_report",
    "validate_certified_artifact",
    "validate_promotion_certificate",
    "validate_promotion_transition",
]
