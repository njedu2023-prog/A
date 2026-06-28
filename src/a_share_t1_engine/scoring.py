from __future__ import annotations

import re
from typing import Any

from .models import ScoredStock, SectorRotation, StockRecord


def score_stocks(records: list[StockRecord], sectors: list[SectorRotation], config: dict[str, Any]) -> list[ScoredStock]:
    scored = [_score_one(record, sectors, config) for record in records]
    return sorted(scored, key=lambda item: stable_sort_key(item, config))


def stable_sort_key(item: ScoredStock, config: dict[str, Any]) -> tuple[float, int, int, float, float, str]:
    grade_order = config["ecs"]["grade_order"]
    grade_index = grade_order.index(item.ecs_grade) if item.ecs_grade in grade_order else len(grade_order)
    return (-item.probability, grade_index, -item.stock.height, -item.iqs, -item.tss, item.stock.code)


def _score_one(record: StockRecord, sectors: list[SectorRotation], config: dict[str, Any]) -> ScoredStock:
    iqs = _calculate_iqs(record, config)
    tss = _calculate_tss(record, sectors, config)
    sas = _calculate_sas(record, config)
    sentiment_adjustment = _calculate_sentiment_adjustment(sas, config)
    event_score = _calculate_event_score(record, config)
    route = _route_for_record(record, config)
    base_probability = float(config["base_probabilities"][route])
    adj = config["probability_adjustments"]
    probability = (
        base_probability
        + (iqs - adj["iqs_center"]) * adj["iqs_weight"]
        + (tss - adj["tss_center"]) * adj["tss_weight"]
        + sentiment_adjustment
        + event_score * adj["event_weight"]
    )
    probability = max(adj["min_probability"], min(adj["max_probability"], probability))
    probability = round(probability, int(config["engine"]["probability_precision"]))
    ecs_score = _calculate_ecs(probability, iqs, tss, sas, event_score, config)
    ecs_grade = _grade_for_score(ecs_score, config)
    return ScoredStock(
        stock=record,
        base_probability=base_probability,
        probability=probability,
        iqs=iqs,
        tss=tss,
        ecs_score=ecs_score,
        ecs_grade=ecs_grade,
        event_score=event_score,
        sas=sas,
        sentiment_adjustment=sentiment_adjustment,
        route=route,
    )


def _route_for_record(record: StockRecord, config: dict[str, Any]) -> str:
    if record.route_override:
        return record.route_override
    return _route_for_height(record.height, config)


def _route_for_height(height: int, config: dict[str, Any]) -> str:
    routes = config["height_routes"]
    if height >= int(routes["n_high_min_height"]):
        return routes["default"]
    return routes.get(str(height), routes["default"])


def _calculate_iqs(record: StockRecord, config: dict[str, Any]) -> float:
    weights = config["iqs"]["weights"]
    norm = config["iqs"]["normalization"]
    quality_score = _board_quality_score(record.board_quality, config)
    order_score = _cap_score(record.order_to_turnover_pct, norm["order_to_turnover_cap_pct"])
    seal_score = _cap_score(record.max_seal_order_yi, norm["max_seal_order_cap_yi"])
    amount_score = _cap_score(record.limit_up_amount_yi, norm["limit_up_amount_cap_yi"])
    turnover_score = _turnover_score(record.turnover_rate_pct, norm["turnover_rate_ideal_pct"], norm["turnover_rate_max_pct"])
    return round(
        quality_score * weights["board_quality"]
        + order_score * weights["order_to_turnover"]
        + seal_score * weights["max_seal_order"]
        + amount_score * weights["limit_up_amount"]
        + turnover_score * weights["turnover_rate"],
        1,
    )


def _calculate_tss(record: StockRecord, sectors: list[SectorRotation], config: dict[str, Any]) -> float:
    tokens = _theme_tokens(record.theme, config)
    matches = []
    for sector in sectors:
        if any(token and (token in sector.name or sector.name in token) for token in tokens):
            matches.append(sector.brs)
    if not matches:
        return float(config["tss"]["unmatched_default"])
    if config["tss"]["aggregation"] == "average":
        return round(sum(matches) / len(matches), 1)
    return round(max(matches), 1)


def _calculate_event_score(record: StockRecord, config: dict[str, Any]) -> float:
    adjustments = config["events"]["score_adjustments"]
    return round(sum(float(adjustments.get(event, 0.0)) for event in record.sensitive_events), 1)


def _calculate_sas(record: StockRecord, config: dict[str, Any]) -> float:
    weights = config["sas"]["weights"]
    norm = config["sas"]["normalization"]
    direction_scores = config["sas"]["direction_scores"]
    ratio_cap = norm["abnormal_ratio_cap"]
    search_score = _abnormal_ratio_score(record.search_abnormal_ratio, ratio_cap)
    discussion_score = _abnormal_ratio_score(record.discussion_abnormal_ratio, ratio_cap)
    direction_score = float(direction_scores.get(record.sentiment_direction, direction_scores["unknown"]))
    credibility_score = _cap_score(record.source_credibility, 1.0)
    return round(
        search_score * weights["search_abnormal"]
        + discussion_score * weights["discussion_abnormal"]
        + direction_score * weights["sentiment_direction"]
        + credibility_score * weights["source_credibility"],
        1,
    )


def _calculate_sentiment_adjustment(sas: float, config: dict[str, Any]) -> float:
    adj = config["probability_adjustments"]
    raw = (sas - adj["sas_center"]) * adj["sas_weight"]
    cap = float(adj["sas_adjustment_cap"])
    return round(max(-cap, min(cap, raw)), 1)


def _calculate_ecs(probability: float, iqs: float, tss: float, sas: float, event_score: float, config: dict[str, Any]) -> float:
    weights = config["ecs"]["weights"]
    normalized_event = max(0.0, min(100.0, 50.0 + event_score * 10.0))
    return round(
        probability * weights["probability"]
        + iqs * weights["iqs"]
        + tss * weights["tss"]
        + sas * weights["sas"]
        + normalized_event * weights["event_score"],
        1,
    )


def _grade_for_score(score: float, config: dict[str, Any]) -> str:
    thresholds = config["ecs"]["grade_thresholds"]
    for grade in config["ecs"]["grade_order"]:
        if score >= float(thresholds[grade]):
            return grade
    return config["ecs"]["grade_order"][-1]


def _board_quality_score(value: str, config: dict[str, Any]) -> float:
    scores = config["iqs"]["board_quality_scores"]
    for key, score in scores.items():
        if key in value:
            return float(score)
    return float(scores["未知"])


def _theme_tokens(theme: str, config: dict[str, Any]) -> list[str]:
    delimiters = config["tss"]["delimiters"]
    pattern = "|".join(re.escape(item) for item in delimiters)
    return [part.strip() for part in re.split(pattern, theme) if part.strip()]


def _cap_score(value: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return max(0.0, min(100.0, value / cap * 100.0))


def _turnover_score(value: float, ideal: float, max_value: float) -> float:
    if value <= 0 or max_value <= ideal:
        return 0.0
    if value <= ideal:
        return _cap_score(value, ideal)
    return max(0.0, 100.0 - (value - ideal) / (max_value - ideal) * 100.0)


def _abnormal_ratio_score(value: float, cap: float) -> float:
    if cap <= 1.0:
        return 50.0
    if value <= 0:
        return 0.0
    if value < 1.0:
        return max(0.0, min(50.0, value * 50.0))
    return max(50.0, min(100.0, 50.0 + (value - 1.0) / (cap - 1.0) * 50.0))
