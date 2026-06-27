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
    height: int = 1
    theme: str = ""
    industry: str = ""
    sensitive_events: list[str] = field(default_factory=list)

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
    route: str
