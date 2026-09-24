from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LALAFO_DISTRICT_FILTERS = (
    ("3-mkr", "30232"),
    ("4-mkr", "30233"),
    ("5-mkr", "30234"),
    ("6-mkr", "30235"),
    ("7-mkr", "30236"),
    ("8-mkr", "30237"),
    ("9-mkr", "30238"),
    ("10-mkr", "30227"),
    ("11-mkr", "30229"),
    ("12-mkr", "30231"),
    ("1000-melochey", "83065"),
    ("auca", "56387"),
    ("ayu-grand", "76159"),
    ("azija-moll", "30239"),
    ("ala-archa-tc", "30392"),
    ("alamedin-1", "23245"),
    ("alamedin-rynok", "23211"),
    ("p-23249-asanbaj", "23249"),
    ("ata-tjurk-park", "30250"),
    ("rajon-bgu", "23205"),
    ("beta-stores-2", "30254"),
    ("beta-stores", "30253"),
    ("bishkek-park-trc", "30256"),
    ("bokonbaevaumetalieva", "45267"),
    ("botanicheskij-sad", "30257"),
    ("verkhny-dzhal", "83060"),
    ("vefa-shopping-center", "56388"),
    ("vostok-5", "23200"),
    ("goin", "27187"),
    ("gosregistr", "76160"),
    ("dordoi-plaza-shopping-center", "56389"),
    ("zagscirk", "45272"),
    ("zolotoj-kvadrat", "45284"),
    ("karavan-trc", "30274"),
    ("1000-melochej-karpinka", "30228"),
    ("manasabokonbaeva", "45274"),
    ("mega-komfort-tc", "30388"),
    ("med-akademiya", "27224"),
    ("molodaya-gvardiya", "45275"),
    ("mossovet", "27222"),
    ("dzhal", "23217"),
    ("novaya-mechet", "45276"),
    ("ortosajskij-rynok", "23202"),
    ("oshskij-rynok", "23212"),
    ("panorama", "30366"),
    ("square-ala-too", "51199"),
    ("victory-square", "82975"),
    ("politekh", "23215"),
    ("sovetskayaskryabina", "45278"),
    ("trc-i-mall", "45280"),
    ("tc-vesna", "45281"),
    ("tec", "27206"),
    ("taatan-tc", "30373"),
    ("tash-rabat", "27204"),
    ("trc-tehnopark", "45279"),
    ("tokoldosh", "23250"),
    ("tunguch", "23206"),
    ("ulan-2-microdistrict", "82971"),
    ("uchkun", "23225"),
    ("filarmoniya", "23214"),
    ("centralnaja-mechet", "30379"),
    ("tserkov", "27207"),
    ("tsum", "23248"),
    ("yug-2", "27210"),
    ("yugnyi-magistral", "56415"),
    ("yunusalievasuvanberdieva", "45283"),
    ("bulvar-erkindik", "45269"),
    ("panfilov-park-spartak-stadium", "82951"),
)

DEFAULT_SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/studio/1-bedroom/owner/"
    "semeynym/param-bez-detey/studentam/bez-podseleniya/"
    "mozhno-s-zhivotnymi"
    "?price[from]=10000&price[to]=50000"
)

TELEGRAM_SOURCE_CHANNELS: tuple[str, ...] = (
    "https://t.me/s/arenda_kvartiry_kg",
    "https://t.me/s/bishkekarendakv",
    "https://t.me/s/house_kg_official",
    "https://t.me/s/apartbishkek",
    "https://t.me/s/kvartirabish",
    "https://t.me/s/wellcometoKGboard",
    "https://t.me/s/Bishkek_kvartira312",
    "https://t.me/s/rental_property_bishkek",
)

# These public channels expose complete photo albums, descriptions and phone
# numbers in Telegram's web preview, so they can supplement Lalafo directly.
TELEGRAM_APARTMENT_CHANNELS: tuple[str, ...] = TELEGRAM_SOURCE_CHANNELS

# Broad searches cover all Bishkek districts and all author types.
ADDITIONAL_SEARCH_URLS: tuple[str, ...] = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/studio/1-bedroom/real-estate-agency"
    "?price[from]=10000&price[to]=50000",
)

_INVENTORY_BASE = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/studio/1-bedroom/"
)
# Split owner results by price as well as location. This exposes older affordable
# ads that can be buried behind the newest broad-search results.
INVENTORY_SEARCH_URLS: tuple[str, ...] = (
    f"{_INVENTORY_BASE}owner?price[from]=10000&price[to]=29999",
    f"{_INVENTORY_BASE}owner?price[from]=30000&price[to]=50000",
    f"{_INVENTORY_BASE}real-estate-agency?price[from]=10000&price[to]=50000",
    # Lalafo authors do not always fill the offerer field. Broad searches keep
    # those fresh cards available; seller classification still happens from
    # the detail page before anything enters the Telegram queue.
    f"{_INVENTORY_BASE}?price[from]=10000&price[to]=29999",
    f"{_INVENTORY_BASE}?price[from]=30000&price[to]=50000",
)

APARTMENT_PUBLISH_INTERVAL_MINUTES = 90


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str = ""
    lalafo_bot_token: str = ""
    # The dedicated link bot runs on the always-on residential Windows PC.
    # Koyeb keeps payment and the main bot only, avoiding webhook/polling races.
    lalafo_bot_cloud_enabled: bool = False
    lalafo_relay_url: str = ""
    lalafo_relay_secret: str = ""
    telegram_group_id: int = -1004389602150
    telegram_bot_username: str = "arenda312bot"
    telegram_extra_channels: str = ""
    admin_user_id: int = 0
    admin_username: str = "maxkgz2"
    support_url: str = "https://t.me/maxkgz2"
    # Payment URLs must be supplied by the deployment.  They are merchant-account
    # specific, so keeping a historical QR here could send money to the wrong
    # account after a configuration change.
    finik_payment_url: str = ""
    monthly_finik_payment_url: str = ""
    wanted_finik_payment_url: str = ""
    # Finik Web SDK. Secrets are configured only in the deployment environment.
    finik_api_url: str = "https://api.acquiring.averspay.kg/v1/payment"
    finik_api_key: str = ""
    finik_account_id: str = ""
    finik_private_key_pem: str = ""
    finik_private_key_b64: str = ""
    finik_webhook_public_key_pem: str = ""
    callback_secret: str = "change-me-in-production"

    lalafo_proxy_url: str = ""
    lalafo_auto_reply_enabled: bool = False
    # Koyeb's shared egress is rejected by Lalafo. The realtime responder runs
    # in its dedicated cloud workflow unless a web host explicitly opts in.
    lalafo_auto_reply_web_enabled: bool = False
    lalafo_login: str = ""
    lalafo_password: str = ""
    lalafo_auto_reply_poll_seconds: float = 10.0
    lalafo_auto_reply_watchdog_seconds: float = 30.0
    lalafo_auto_reply_stale_seconds: float = 180.0
    hosted_apartment_scheduler_enabled: bool = True
    hosted_apartment_scheduler_check_seconds: float = 60.0
    # The free Koyeb web instance sleeps after one hour without inbound HTTP
    # traffic.  A small request through the public URL keeps the webhook and
    # Lalafo responder warm without requiring a paid worker service.
    service_keepalive_enabled: bool = True
    service_keepalive_seconds: float = 900.0
    service_keepalive_timeout_seconds: float = 15.0
    background_watchdog_seconds: float = 30.0
    # Fixed product schedule. Keeping this outside environment-controlled
    # settings prevents a stale cloud variable from restoring the old cadence.
    hosted_apartment_publish_interval_minutes: int = APARTMENT_PUBLISH_INTERVAL_MINUTES
    apartment_detail_concurrency: int = 6
    # Keep each public album adjacent to its card. Speed comes from direct
    # Telegram URL fetches and concurrent Lalafo detail collection.
    apartment_publish_concurrency: int = 1
    apartment_cycle_timeout_seconds: float = 4_200.0
    apartment_publication_lease_seconds: int = 300
    apartment_publication_heartbeat_seconds: float = 60.0
    city: str = "Бишкек"
    min_price: int = 10_000
    max_price: int = 50_000
    rooms: str = "studio,1"
    max_new_posts_per_run: int = 18
    max_search_pages: int = 36
    preferred_districts_only: bool = False
    # Telegram accepts at most ten photos per media group. Publishers split
    # longer listings into adjacent groups so no Lalafo photo is dropped.
    max_photos_per_apartment: int = 10
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

    run_trigger_secret: str = ""
    run_bot: bool = False
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""

    log_level: str = "INFO"

    @field_validator("admin_user_id", mode="before")
    @classmethod
    def empty_admin_id(cls, value: object) -> object:
        return 0 if value in (None, "") else value

    @field_validator("hosted_apartment_publish_interval_minutes", mode="before")
    @classmethod
    def fixed_apartment_publish_interval(cls, value: object) -> int:
        del value
        return APARTMENT_PUBLISH_INTERVAL_MINUTES

    @field_validator("telegram_bot_username", "admin_username", mode="before")
    @classmethod
    def strip_at(cls, value: object) -> object:
        return value.lstrip("@") if isinstance(value, str) else value

    @property
    def effective_post_limit(self) -> int:
        return 1 if self.test_mode else max(1, self.max_new_posts_per_run)

    @property
    def support_bot_url(self) -> str:
        """Always keep support inside the customer bot, regardless of stale env URLs."""
        username = self.telegram_bot_username.lstrip("@")
        return f"https://t.me/{username}?start=support"

    @property
    def telegram_source_channels(self) -> tuple[str, ...]:
        extra = tuple(
            value.strip() for value in self.telegram_extra_channels.split(",") if value.strip()
        )
        return tuple(dict.fromkeys((*TELEGRAM_SOURCE_CHANNELS, *extra)))

    @property
    def allowed_rooms(self) -> tuple[str, ...]:
        return tuple(part.strip().lower() for part in self.rooms.split(",") if part.strip())

    def require_bot_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for this operation")
        return self.telegram_bot_token

    def require_lalafo_bot_token(self) -> str:
        if not self.lalafo_bot_token:
            raise RuntimeError("LALAFO_BOT_TOKEN is not configured")
        return self.lalafo_bot_token

    def require_lalafo_auto_reply_credentials(self) -> tuple[str, str]:
        if not self.lalafo_login or not self.lalafo_password:
            raise RuntimeError(
                "LALAFO_LOGIN and LALAFO_PASSWORD are required when auto-reply is enabled"
            )
        return self.lalafo_login, self.lalafo_password

    def require_callback_secret(self) -> str:
        if not self.callback_secret or self.callback_secret == "change-me-in-production":
            raise RuntimeError("Set a strong CALLBACK_SECRET before running the bot")
        return self.callback_secret

    def require_run_trigger_secret(self) -> str:
        if len(self.run_trigger_secret) < 32:
            raise RuntimeError("RUN_TRIGGER_SECRET must contain at least 32 characters")
        return self.run_trigger_secret

    def require_telegram_webhook_url(self) -> str:
        if not self.telegram_webhook_url.startswith("https://"):
            raise RuntimeError("TELEGRAM_WEBHOOK_URL must be an HTTPS URL")
        return self.telegram_webhook_url.rstrip("/")

    def require_telegram_webhook_secret(self) -> str:
        secret = self.telegram_webhook_secret
        if len(secret) < 32 or not all(char.isalnum() or char in "_-" for char in secret):
            raise RuntimeError(
                "TELEGRAM_WEBHOOK_SECRET must contain at least 32 URL-safe characters"
            )
        return secret

    def require_public_base_url(self) -> str:
        webhook_url = self.require_telegram_webhook_url()
        suffix = "/telegram/webhook"
        if not webhook_url.endswith(suffix):
            raise RuntimeError(f"TELEGRAM_WEBHOOK_URL must end with {suffix}")
        return webhook_url[: -len(suffix)]

    @property
    def finik_auto_enabled(self) -> bool:
        return bool(
            self.finik_api_key
            and self.finik_account_id
            and (self.finik_private_key_pem or self.finik_private_key_b64)
            and self.telegram_webhook_url
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
