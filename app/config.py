from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/2-bedrooms/studio/owner/"
    "real-estate-agency/bez-podseleniya?price[to]=35000"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str = ""
    telegram_group_id: int = -1004389602150
    telegram_bot_username: str = ""
    admin_user_id: int = 0
    admin_username: str = "maxkgz2"
    support_url: str = "https://t.me/maxkgz2"
    finik_payment_url: str = (
        "https://qr.finik.kg/e4a0a0da-51b8-42f5-8537-aa4c9403aad6"
    )
    callback_secret: str = "change-me-in-production"

    lalafo_search_url: str = DEFAULT_SEARCH_URL
    city: str = "Бишкек"
    max_price: int = 35_000
    rooms: str = "studio,1,2"
    max_new_posts_per_run: int = 10
    max_photos_per_apartment: int = 5
    only_with_photos: bool = True
    allow_no_deposit: bool = True
    allow_no_district: bool = True
    dry_run: bool = False
    test_mode: bool = False

    database_url: str = "sqlite:///data/bot.db"
    posted_state_path: Path = Path("data/posted_ads.json")
    state_retention_days: int = 90
    http_timeout_seconds: float = 25.0
    http_max_retries: int = 3

    log_level: str = "INFO"

    @field_validator("admin_user_id", mode="before")
    @classmethod
    def empty_admin_id(cls, value: object) -> object:
        return 0 if value in (None, "") else value

    @field_validator("telegram_bot_username", "admin_username", mode="before")
    @classmethod
    def strip_at(cls, value: object) -> object:
        return value.lstrip("@") if isinstance(value, str) else value

    @property
    def effective_post_limit(self) -> int:
        return 1 if self.test_mode else max(1, self.max_new_posts_per_run)

    @property
    def allowed_rooms(self) -> tuple[str, ...]:
        return tuple(part.strip().lower() for part in self.rooms.split(",") if part.strip())

    def require_bot_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for this operation")
        return self.telegram_bot_token

    def require_callback_secret(self) -> str:
        if not self.callback_secret or self.callback_secret == "change-me-in-production":
            raise RuntimeError("Set a strong CALLBACK_SECRET before running the bot")
        return self.callback_secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
