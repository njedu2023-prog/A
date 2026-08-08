from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


FEATURE_CONTRACT_VERSION = "feature_contract_v2_1"


class FieldRole(str, Enum):
    ALPHA = "ALPHA"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    QUALITY = "QUALITY"
    AUXILIARY = "AUXILIARY"


class AsOfPolicy(str, Enum):
    D_CLOSE = "D_CLOSE"
    D_SOURCE_SNAPSHOT = "D_SOURCE_SNAPSHOT"
    D_DERIVED = "D_DERIVED"


class MissingPolicy(str, Enum):
    REQUIRED_FOR_COVERAGE = "REQUIRED_FOR_COVERAGE"
    AT_LEAST_ONE_IN_GROUP = "AT_LEAST_ONE_IN_GROUP"
    OPTIONAL_MODEL_DEFAULT = "OPTIONAL_MODEL_DEFAULT"
    DERIVED = "DERIVED"


class TrustLevel(str, Enum):
    LOCAL_POINT_IN_TIME = "LOCAL_POINT_IN_TIME"
    PINNED_SOURCE = "PINNED_SOURCE"
    UPSTREAM_DECLARED = "UPSTREAM_DECLARED"
    LOCAL_DERIVED = "LOCAL_DERIVED"


@dataclass(frozen=True)
class FeatureFieldContract:
    """Point-in-time contract for one current production feature.

    ``coverage_group`` is deliberately independent of the field role. Multiple
    fields may satisfy one group (currently amount or volume can satisfy the
    liquidity group), while optional predictors remain visible in the contract
    without silently changing the established production coverage score.
    """

    name: str
    role: FieldRole
    source: str
    asof: AsOfPolicy
    missing_policy: MissingPolicy
    trust_level: TrustLevel
    coverage_group: str | None = None


def _field(
    name: str,
    role: FieldRole,
    source: str,
    asof: AsOfPolicy,
    missing_policy: MissingPolicy = MissingPolicy.OPTIONAL_MODEL_DEFAULT,
    trust_level: TrustLevel = TrustLevel.LOCAL_POINT_IN_TIME,
    *,
    coverage_group: str | None = None,
) -> FeatureFieldContract:
    return FeatureFieldContract(
        name=name,
        role=role,
        source=source,
        asof=asof,
        missing_policy=missing_policy,
        trust_level=trust_level,
        coverage_group=coverage_group,
    )


def _build_contract() -> dict[str, FeatureFieldContract]:
    fields: list[FeatureFieldContract] = []

    market_roles = {
        "ret_1d": FieldRole.ALPHA,
        "ret_3d": FieldRole.ALPHA,
        "ret_5d": FieldRole.ALPHA,
        "ret_10d": FieldRole.ALPHA,
        "ret_20d": FieldRole.ALPHA,
        "volatility_5d": FieldRole.RISK,
        "volatility_20d": FieldRole.RISK,
        "downside_volatility_20d": FieldRole.RISK,
        "atr_14d": FieldRole.RISK,
        "amplitude_20d": FieldRole.RISK,
        "cvar_loss_10pct": FieldRole.RISK,
        "max_drawdown_20d": FieldRole.RISK,
        "avg_amount_20d": FieldRole.EXECUTION,
        "avg_volume_20d": FieldRole.EXECUTION,
        "avg_turnover_20d": FieldRole.EXECUTION,
        "bar_count": FieldRole.QUALITY,
    }
    coverage_fields = {
        "ret_5d",
        "ret_20d",
        "volatility_20d",
        "downside_volatility_20d",
        "atr_14d",
        "amplitude_20d",
        "cvar_loss_10pct",
        "max_drawdown_20d",
    }
    for name, role in market_roles.items():
        if name in coverage_fields:
            missing_policy = MissingPolicy.REQUIRED_FOR_COVERAGE
            coverage_group = name
        elif name in {"avg_amount_20d", "avg_volume_20d"}:
            missing_policy = MissingPolicy.AT_LEAST_ONE_IN_GROUP
            coverage_group = "liquidity_20d"
        else:
            missing_policy = MissingPolicy.OPTIONAL_MODEL_DEFAULT
            coverage_group = None
        fields.append(
            _field(
                name,
                role,
                "market_daily_qfq",
                AsOfPolicy.D_CLOSE,
                missing_policy,
                coverage_group=coverage_group,
            )
        )

    rank_roles = {
        "rank_percentile_a_top10": FieldRole.ALPHA,
        "rank_percentile_premium_top10": FieldRole.ALPHA,
        "rank_percentile_decision_table": FieldRole.ALPHA,
        "rank_borda": FieldRole.ALPHA,
        "rank_consensus": FieldRole.ALPHA,
        "rank_disagreement": FieldRole.RISK,
    }
    fields.extend(
        _field(
            name,
            role,
            "three_table_ranks",
            AsOfPolicy.D_DERIVED,
            trust_level=TrustLevel.LOCAL_DERIVED,
        )
        for name, role in rank_roles.items()
    )

    fields.extend(
        _field(
            name,
            FieldRole.AUXILIARY,
            "decision_stage",
            AsOfPolicy.D_DERIVED,
            trust_level=TrustLevel.PINNED_SOURCE,
        )
        for name in (
            "stage_transition",
            "stage_from",
            "stage_to",
            "stage_is_2_to_3",
            "stage_is_3_to_4",
        )
    )
    fields.extend(
        (
            _field(
                "source_strength",
                FieldRole.ALPHA,
                "three_table_predictions",
                AsOfPolicy.D_DERIVED,
                trust_level=TrustLevel.LOCAL_DERIVED,
            ),
            _field(
                "feature_coverage",
                FieldRole.QUALITY,
                "feature_contract",
                AsOfPolicy.D_DERIVED,
                MissingPolicy.DERIVED,
                TrustLevel.LOCAL_DERIVED,
            ),
        )
    )

    upstream_roles = {
        "src_a_top10__prob_final": FieldRole.ALPHA,
        "src_a_top10__p_limit_up_calibrated": FieldRole.AUXILIARY,
        "src_a_top10__auction_strength_score": FieldRole.ALPHA,
        "src_premium_top10__premium_rank_score": FieldRole.ALPHA,
        "src_premium_top10__t_limitup_prob_calibrated": FieldRole.AUXILIARY,
        "src_premium_top10__t_close_ret_pred": FieldRole.ALPHA,
        "src_premium_top10__t1_accept_prob_blend": FieldRole.AUXILIARY,
        "src_premium_top10__t1_fail_prob_blend": FieldRole.RISK,
        "src_decision_table__decision_p_fill": FieldRole.EXECUTION,
        "src_decision_table__decision_e_ret": FieldRole.ALPHA,
        "src_decision_table__decision_ev": FieldRole.ALPHA,
        "src_decision_table__predicted_fill_probability": FieldRole.EXECUTION,
        "src_decision_table__predicted_net_return": FieldRole.ALPHA,
        "src_decision_table__predicted_exit_delay_probability": FieldRole.RISK,
        "src_decision_table__predicted_continuation_limit_up_probability": FieldRole.AUXILIARY,
    }
    for name, role in upstream_roles.items():
        source = name.removeprefix("src_").split("__", 1)[0]
        fields.append(
            _field(
                name,
                role,
                source,
                AsOfPolicy.D_SOURCE_SNAPSHOT,
                trust_level=TrustLevel.UPSTREAM_DECLARED,
            )
        )

    result: dict[str, FeatureFieldContract] = {}
    for item in fields:
        if item.name in result:
            raise RuntimeError(f"duplicate feature contract: {item.name}")
        result[item.name] = item
    return result


PRODUCTION_FEATURE_CONTRACT: Mapping[str, FeatureFieldContract] = MappingProxyType(
    _build_contract()
)


def feature_contract_payload() -> dict[str, Any]:
    """Return the canonical, serialisable production contract."""

    return {
        "schema_version": FEATURE_CONTRACT_VERSION,
        "fields": [
            {
                "name": spec.name,
                "role": spec.role.value,
                "source": spec.source,
                "asof": spec.asof.value,
                "missing_policy": spec.missing_policy.value,
                "trust_level": spec.trust_level.value,
                "coverage_group": spec.coverage_group,
            }
            for spec in sorted(
                PRODUCTION_FEATURE_CONTRACT.values(),
                key=lambda item: item.name,
            )
        ],
    }


def feature_contract_sha256() -> str:
    encoded = json.dumps(
        feature_contract_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def production_feature_coverage(values: Mapping[str, Any]) -> float:
    """Return coverage across contract-declared required groups.

    A genuine numeric zero is observed data. Only ``None`` is missing, matching
    the existing feature builder semantics and avoiding any ranking change.
    """

    groups: dict[str, bool] = {}
    for spec in PRODUCTION_FEATURE_CONTRACT.values():
        if spec.coverage_group is None:
            continue
        groups.setdefault(spec.coverage_group, False)
        if values.get(spec.name) is not None:
            groups[spec.coverage_group] = True
    if not groups:
        return 1.0
    return sum(groups.values()) / len(groups)


__all__ = [
    "FEATURE_CONTRACT_VERSION",
    "PRODUCTION_FEATURE_CONTRACT",
    "AsOfPolicy",
    "FeatureFieldContract",
    "FieldRole",
    "MissingPolicy",
    "TrustLevel",
    "feature_contract_payload",
    "feature_contract_sha256",
    "production_feature_coverage",
]
