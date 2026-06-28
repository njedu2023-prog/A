from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StockRecord:
    code: str
    name: str = ""
    list_sources: set[str] = field(default_factory=set)
    board_quality: str = "未知"
    order_to_turnover_pct: float = 0.0
    max_seal_order_yi: float = 0.0
    limit_up_amount_yi: float = 0.0
    turnover_rate_pct: float = 0.0
    first_limit_up_time: str = ""
    final_limit_up_time: str = ""
    height: int = 1
    theme: str = ""
    industry: str = ""
    route_override: str | None = None
    sensitive_events: list[str] = field(default_factory=list)
    search_abnormal_ratio: float = 1.0
    discussion_abnormal_ratio: float = 1.0
    sentiment_direction: str = "neutral"
    source_credibility: float = 0.5
    auction_change_pct: float = 0.0
    auction_amount_yi: float = 0.0
    auction_seal_order_yi: float = 0.0
    opening_5m_amount_yi: float = 0.0
    fast_reseal: bool = False

    @property
    def overlap_flag(self) -> str:
        return "是" if len(self.list_sources) > 1 else "否"


@dataclass(frozen=True)
class SectorRotation:
    name: str
    net_flow_amount_yi: float = 0.0
    limit_up_count: int = 0
    heat_token: str = "中性"
    brs: float = 50.0
    heat_value: float = 0.0


@dataclass(frozen=True)
class ReportMetadata:
    auction_buy_date: str = ""
    auction_buy_date_iso: str = ""
    t1_sell_date: str = ""


@dataclass(frozen=True)
class ScoredStock:
    stock: StockRecord
    base_probability: float
    probability: float
    iqs: float
    tss: float
    ecs_score: float
    ecs_grade: str
    event_score: float
    sas: float
    sentiment_adjustment: float
    route: str
