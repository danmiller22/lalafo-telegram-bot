from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.lalafo.client import LalafoClient
    from app.lalafo.models import LalafoAd, SearchAd, SearchPage

__all__ = ["LalafoAd", "LalafoClient", "SearchAd", "SearchPage"]


def __getattr__(name: str) -> Any:
    """Keep the public API without importing scraper dependencies eagerly."""
    if name == "LalafoClient":
        from app.lalafo.client import LalafoClient

        return LalafoClient
    if name in {"LalafoAd", "SearchAd", "SearchPage"}:
        from app.lalafo import models

        return getattr(models, name)
    raise AttributeError(name)
