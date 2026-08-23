from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.lalafo.models import LalafoAd


def ad_fingerprint(ad: LalafoAd) -> str:
    raw = "|".join((ad.phone, str(ad.price), (ad.district or "").casefold()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class PostedState:
    path: Path
    items: list[dict[str, object]]

    @classmethod
    def load(cls, path: Path) -> "PostedState":
        if not path.exists():
            return cls(path=path, items=[])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(f"State file is invalid: {path}")
        return cls(path=path, items=list(payload.get("items") or []))

    def contains(self, lalafo_id: int, fingerprint: str | None = None) -> bool:
        return any(
            int(item.get("lalafo_id") or 0) == lalafo_id
            or (fingerprint and item.get("fingerprint") == fingerprint)
            for item in self.items
        )

    def add(self, ad: LalafoAd, *, telegram_message_id: int) -> None:
        self.items.append(
            {
                "lalafo_id": ad.lalafo_id,
                "fingerprint": ad_fingerprint(ad),
                "published_at": datetime.now(timezone.utc).isoformat(),
                "telegram_message_id": telegram_message_id,
            }
        )

    def prune(self, retention_days: int) -> None:
        threshold = datetime.now(timezone.utc) - timedelta(days=retention_days)
        kept: list[dict[str, object]] = []
        for item in self.items:
            try:
                published = datetime.fromisoformat(str(item["published_at"]))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if published >= threshold:
                kept.append(item)
        self.items = kept

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": self.items,
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)
