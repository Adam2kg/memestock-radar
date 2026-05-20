from dataclasses import dataclass, field


@dataclass
class RedditSignal:
    ticker: str
    mentions: int
    upvotes: int
    sentiment_score: float
    sentiment_label: str
    subreddit_count: int
    subreddit_breakdown: dict
    hype_index: float
    engagement_ratio: float = 1.0  # avg comments-per-post for ticker / subreddit baseline

    @property
    def is_bullish(self) -> bool:
        return self.sentiment_score >= 0.05

    @property
    def is_bearish(self) -> bool:
        return self.sentiment_score <= -0.05
