from __future__ import annotations

from app.lalafo.models import LalafoAd


def halve_subletting_candidates(candidates: list[LalafoAd]) -> list[LalafoAd]:
    """Keep all whole apartments and a deterministic half of shared listings."""
    shared = sorted(
        (ad for ad in candidates if not ad.no_subletting),
        key=lambda ad: ad.lalafo_id,
    )
    kept_shared_ids = {ad.lalafo_id for ad in shared[::2]}
    return [
        ad
        for ad in candidates
        if ad.no_subletting or ad.lalafo_id in kept_shared_ids
    ]
