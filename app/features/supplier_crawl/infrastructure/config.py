"""Collector limits only; no provider settings or environment loading."""
from pydantic import BaseModel, ConfigDict, Field


class CrawlSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    crawl_user_agent: str = 'GyeonjwoSupplierCrawler/1.0'
    crawl_delay_seconds: float = Field(default=1.0, ge=0.5, le=10)
    crawl_timeout_seconds: float = Field(default=10, ge=3, le=30)
    crawl_max_pages_per_site: int = Field(default=4, ge=1, le=10)
