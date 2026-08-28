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
    ("10-mkr", "30227"),
    ("11-mkr", "30229"),
    ("12-mkr", "30231"),
    ("1000-melochey", "83065"),
    ("auca", "56387"),
    ("ayu-grand", "76159"),
    ("azija-moll", "30239"),
    ("ak-keme-staryj-ajeroport", "30241"),
    ("ak-orgo", "23231"),
    ("academy-of-sciences", "82946"),
    ("ala-archa-2-town", "82942"),
    ("ala-archa-3-town", "82943"),
    ("ala-archa-tc", "30392"),
    ("ala-too", "23234"),
    ("alamedin-1", "23245"),
    ("alamedin-rynok", "23211"),
    ("archa-beshik", "23207"),
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
    ("vostochnyi-avtovokzal", "27186"),
    ("goin", "27187"),
    ("energetiki", "23204"),
    ("gorodok-stroitelej", "30262"),
    ("gorodskaja-bolnica-4-ul-ajni", "30263"),
    ("gosregistr", "76160"),
    ("dvorec-sporta", "30265"),
    ("dzhal-15", "83062"),
    ("dzhal-29", "83063"),
    ("dordoj-motors-rynok", "30267"),
    ("dordoi-plaza-shopping-center", "56389"),
    ("dordoj", "23210"),
    ("zagscirk", "45272"),
    ("zapadnyi-avtovokzal", "27192"),
    ("zarya-bishkek", "70957"),
    ("zolotoj-kvadrat", "45284"),
    ("karavan-trc", "30274"),
    ("1000-melochej-karpinka", "30228"),
    ("kirkomstrom", "27198"),
    ("kirpichnyj-zavod", "30277"),
    ("kok-zhar", "23228"),
    ("magazin-kosmos", "45273"),
    ("madina", "23209"),
    ("manasabokonbaeva", "45274"),
    ("mega-komfort-tc", "30388"),
    ("med-akademiya", "27224"),
    ("molodaya-gvardiya", "45275"),
    ("mossovet", "27222"),
    ("dzhal", "23217"),
    ("ortosajskij-rynok", "23202"),
    ("oshskij-rynok", "23212"),
    ("pishpek", "23218"),
    ("square-ala-too", "51199"),
    ("victory-square", "82975"),
    ("politekh", "23215"),
    ("sovetskayaskryabina", "45278"),
    ("staryj-tolchok", "23253"),
    ("trc-i-mall", "45280"),
    ("tc-vesna", "45281"),
    ("tec", "27206"),
    ("taatan-tc", "30373"),
    ("tash-rabat", "27204"),
    ("trc-tehnopark", "45279"),
    ("tokoldosh", "23250"),
    ("tunguch", "23206"),
    ("ulan-2-microdistrict", "82971"),
    ("mcr-ulan", "27199"),
    ("umetalievafrunze", "45282"),
    ("uchkun", "23225"),
    ("physical-education-ksafkis", "82983"),
    ("fizpribory", "30378"),
    ("filarmoniya", "23214"),
    ("centralnaja-mechet", "30379"),
    ("tsum", "23248"),
    ("yug-2", "27210"),
    ("yugnyi-magistral", "56415"),
    ("yunusalievasuvanberdieva", "45283"),
    ("bulvar-erkindik", "45269"),
    ("panfilov-park-spartak-stadium", "82951"),
    ("st-almatinka-chuy", "56395"),
    ("st-akhunbaeva-bakaeva", "56396"),
    ("st-akhunbaeva-maldybaeva", "56397"),
    ("st-akhunbaeva-chapaeva", "56398"),
    ("st-bokonbaev-gogol", "56401"),
    ("gagarina-st", "82996"),
    ("st-gogol-chuy", "56402"),
    ("gorkogo-st-almatinka-st", "82991"),
    ("gorkogo-st-panfilova-st", "82990"),
    ("st-kalyk-akieva", "56403"),
    ("st-kyiv-umetalieva", "56404"),
    ("kulatova-st-matrosova-st", "82992"),
    ("st-leo-tolstoy", "56405"),
    ("st-logvinenko-bokonbaeva", "56406"),
    ("st-moscow-gogol", "56407"),
    ("st-moscow-turusbekova", "56409"),
    ("moskovskaya-st-umetalieva-st", "82958"),
    ("st-ogonbaeva-gogol", "56410"),
    ("orozbekova-st-zhibek-zholu-st", "82994"),
    ("st-chyngyz-aitmatova", "56412"),
)

DEFAULT_SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/2-bedrooms/studio/"
    + "/".join(alias for alias, _ in LALAFO_DISTRICT_FILTERS)
    + "?price[to]=35000"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str = ""
    telegram_group_id: int = -1004389602150
    telegram_bot_username: str = "arenda312bot"
    admin_user_id: int = 0
    admin_username: str = "maxkgz2"
    support_url: str = "https://t.me/maxkgz2"
    finik_payment_url: str = (
        "https://qr.finik.kg/#00020101021232810011qr.finik.kg0114averspay-items1"
        "032f51f3d85c0da4eeab638f8ed65f0a3831202121302125204482953034175405"
        "500005908Finik-QR6304e37c"
    )
    wanted_finik_payment_url: str = (
        "https://qr.finik.kg/#00020101021232810011qr.finik.kg0114averspay-items1"
        "032946f1ce95e414d5b965a9e8574a95f361202121302125204482953034175405"
        "100005908Finik-QR63042d14"
    )
    callback_secret: str = "change-me-in-production"

    lalafo_search_url: str = DEFAULT_SEARCH_URL
    lalafo_proxy_url: str = ""
    lalafo_auto_reply_enabled: bool = False
    lalafo_login: str = ""
    lalafo_password: str = ""
    lalafo_auto_reply_poll_seconds: float = 10.0
    lalafo_auto_reply_watchdog_seconds: float = 30.0
    lalafo_auto_reply_stale_seconds: float = 180.0
    hosted_apartment_scheduler_enabled: bool = True
    hosted_apartment_scheduler_check_seconds: float = 300.0
    hosted_apartment_publish_interval_minutes: int = 120
    city: str = "Бишкек"
    min_price: int = 8_000
    max_price: int = 35_000
    rooms: str = "studio,1,2"
    max_new_posts_per_run: int = 80
    repost_after_hours: float = 2.0
    max_search_pages: int = 50
    preferred_districts_only: bool = False
    max_photos_per_apartment: int = 5
    only_with_photos: bool = True
    allow_no_deposit: bool = True
    allow_no_district: bool = False
    dry_run: bool = False
    test_mode: bool = False

    # Isolated daily featured-ad automation. Both mutating switches are off by
    # default so deploying the code cannot publish or spend money by itself.
    featured_lalafo_enabled: bool = False
    featured_publish_approved_enabled: bool = False
    featured_autopromote_enabled: bool = False
    featured_count: int = 2
    featured_priority_price: int = 30_000
    featured_max_price: int = 35_000
    featured_daily_budget_per_ad: int = 200
    featured_max_daily_budget: int = 400
    featured_min_wallet_balance: int = 400
    featured_cooldown_days: int = 7
    featured_max_candidates: int = 10
    featured_timezone: str = "Asia/Bishkek"
    featured_max_photos: int = 10
    featured_require_repost_permission: bool = True
    featured_review_enabled: bool = False
    featured_auto_select_enabled: bool = False
    featured_review_bot_token: str = ""

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

    @field_validator("telegram_bot_username", "admin_username", mode="before")
    @classmethod
    def strip_at(cls, value: object) -> object:
        return value.lstrip("@") if isinstance(value, str) else value

    @property
    def effective_post_limit(self) -> int:
        return 1 if self.test_mode else max(80, self.max_new_posts_per_run)

    @property
    def allowed_rooms(self) -> tuple[str, ...]:
        return tuple(part.strip().lower() for part in self.rooms.split(",") if part.strip())

    def require_bot_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for this operation")
        return self.telegram_bot_token

    def require_featured_review_bot_token(self) -> str:
        if not self.featured_review_bot_token:
            raise RuntimeError("FEATURED_REVIEW_BOT_TOKEN is required")
        return self.featured_review_bot_token

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
