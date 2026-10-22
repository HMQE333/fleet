"""Runtime configuration, read from environment variables and `.env`."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Export .env into the process (e.g. OPENROUTER_API_KEY / FLEET_MASTER_KEY on servers without a keyring).
load_dotenv()

# browser-use reads these at import time; keep everything local (RULES.md §8).
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FLEET_", extra="ignore")

    # A Postgres DSN, or "embedded": Fleet's own Postgres under data/pg (fleet/db.py, needs the pgserver package).
    database_url: str = "postgresql://postgres@localhost:5432/fleet"
    data_dir: Path = Path("data")

    # Readiness checks (READINESS.md): the most the live gates may spend in one run.
    readiness_budget_usd: float = 0.25

    # API
    host: str = "127.0.0.1"
    port: int = 8710
    api_token: str = ""  # when set, every API call needs `Authorization: Bearer <token>`
    allowed_hosts: str = ""  # extra hostnames the API answers to (comma-separated), e.g. for a cloud deploy

    # Models run through OpenRouter; which model does which job is set in the panel (fleet/models.py).
    openrouter_url: str = "https://openrouter.ai/api/v1"

    # AdsPower Local API (AdsPower profiles only)
    adspower_url: str = "http://local.adspower.net:50325"
    # Local profiles: the browser Fleet starts itself. Empty = the first Chrome or Edge found (fleet/browsers.py).
    browser_exe: str = ""
    close_browser_after_subtask: bool = True

    # Worker capacity
    worker_web_slots: int = 6  # fetch + research subtasks (no profile needed)
    worker_browser_slots: int = 4  # upper bound; real limit is the number of idle profiles

    # Limits
    max_browser_steps: int = 60
    human_wait_seconds: int = 1800
    lease_seconds: int = 120
    fetch_min_delay_seconds: float = 2.0
    scrape_max_pages: int = 10  # a scrape step follows at most this many pages ...
    scrape_max_items: int = 300  # ... and keeps at most this many items
    scrape_max_seconds: int = 300  # ... and stops starting new pages after this long
    scrape_max_details: int = 30  # item pages opened per scrape step for what the list page doesn't show
    crawl_max_pages: int = 50  # a crawl step reads at most this many pages of one site section ...
    crawl_max_depth: int = 3  # ... at most this many clicks from the start page ...
    crawl_max_seconds: int = 300  # ... and stops starting new pages after this long
    crawl_text_chars: int = 5000  # text kept per page
    retention_days: int = 0  # screenshots, histories and step logs of finished tasks older than this are cleaned up (0 = keep)
    registrations_per_day: int = 3  # accounts Fleet may create in 24 hours, across all tasks

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def histories_dir(self) -> Path:
        return self.data_dir / "histories"


@lru_cache
def settings() -> Settings:
    s = Settings()
    s.screenshots_dir.mkdir(parents=True, exist_ok=True)
    s.histories_dir.mkdir(parents=True, exist_ok=True)
    return s
