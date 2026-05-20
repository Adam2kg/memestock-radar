from dataclasses import dataclass
from .reddit_signal import RedditSignal
from .news_signal import NewsSignal


@dataclass
class TradeCandidate:
    ticker: str
    reddit: RedditSignal
    news: NewsSignal
    composite_score: float
    confidence: float
    direction: str          # long | short | contrarian_long | fade_retail | hold
    interpretation: str     # human-readable label for the 2x2 quadrant
    momentum: float         # delta vs rolling avg
    rationale: str
    is_novel: bool = False  # ticker absent from recent history (gem signal)
    gem_score: float = 0.0  # set by find_hidden_gems(); 0 if not a gem
