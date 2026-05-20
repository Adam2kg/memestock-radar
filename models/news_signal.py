from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class NewsSignal:
    ticker: str
    articles: List[Dict] = field(default_factory=list)
    sentiment_avg: float = 0.0
    sentiment_weighted: float = 0.0  # weighted by recency
    volume: int = 0
    finnhub_buzz: float = 0.0  # native buzz score when available
    finnhub_company_score: float = 0.0  # native sentiment when available

    @property
    def is_bullish(self) -> bool:
        return self.sentiment_weighted >= 0.05

    @property
    def is_bearish(self) -> bool:
        return self.sentiment_weighted <= -0.05
