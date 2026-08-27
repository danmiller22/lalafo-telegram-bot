from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpeg",
    "image/pjpeg": ".jpeg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def image_upload_metadata(content: bytes, advertised_type: str) -> tuple[str, str]:
    """Use byte signatures because image CDNs may advertise an unusable MIME type."""
    if content.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        content_type = "image/webp"
    else:
        content_type = advertised_type.split(";", 1)[0].strip().casefold()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ManagedAdsContractError(
            f"Source image has unsupported content type: {content_type or 'unknown'}"
        )
    return f"apartment{ALLOWED_IMAGE_TYPES[content_type]}", content_type


def clean_jpeg_for_upload(content: bytes) -> bytes:
    """Re-encode source photos so Lalafo receives a canonical, EXIF-free JPEG."""
    try:
        with Image.open(BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode != "RGB":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ManagedAdsContractError("Source image cannot be decoded") from exc


class ManagedAdsError(RuntimeError):
    pass


class ManagedAdsAuthenticationError(ManagedAdsError):
    pass


class ManagedAdsContractError(ManagedAdsError):
    pass


class ManagedAdsAmbiguousResultError(ManagedAdsError):
    """The request may have reached Lalafo, so blindly retrying is unsafe."""


LALAFO_PUBLICATION_STATUSES = {
    1: "moderation",
    2: "active",
    3: "rejected",
    4: "banned",
    7: "creating",
    8: "deactivated",
    11: "payment_waiting",
}


def publication_status(payload: dict[str, Any]) -> str:
    """Return Lalafo's authoritative visibility state for an owned ad.

    The web client currently uses numeric ``status_id`` values.  Some API
    responses wrap the ad in ``data`` or ``ad``, so unwrap those shapes while
    keeping an unknown response safe rather than assuming that it is public.
    """
    current: Any = payload
    for _ in range(3):
        if not isinstance(current, dict):
            return "unknown"
        if "status_id" in current:
            try:
                return LALAFO_PUBLICATION_STATUSES.get(
                    int(current["status_id"]), "unknown"
                )
            except (TypeError, ValueError):
                return "unknown"
        nested = current.get("data") or current.get("ad")
        if nested is current:
            break
        current = nested
    return "unknown"


@dataclass(slots=True)
class ManagedSession:
    profile_id: int
    token: str
    access_token: str


class LalafoManagedAdsClient:
    """Dedicated authenticated session that deliberately exposes no chat routes."""

    def __init__(self, *, timeout: float = 25.0) -> None:
        self._user_hash = str(uuid.uuid4())
        self._fingerprint = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        self._http = httpx.AsyncClient(
            base_url="https://lalafo.kg", follow_redirects=True,
            timeout=httpx.Timeout(timeout),
        )
        self._images = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(timeout),
            headers={"Accept": "image/jpeg,image/png,image/webp"},
        )
        self.session: ManagedSession | None = None

    def _headers(self, token: str = "", *, json: bool = True) -> dict[str, str]:
        headers = {
            "device": "pc", "language": "ru_RU", "country-id": "12",
            "request-id": f"react-client-{uuid.uuid4()}",
            "Authorization": f"Bearer {token}" if token else "",
            "user-hash": self._user_hash,
            "device-fingerprint": self._fingerprint,
            "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/140 Safari/537.36",
        }
        if json:
            headers["content-type"] = "application/json"
        return headers

    def _token(self) -> str:
        if self.session is None:
            raise ManagedAdsAuthenticationError("Managed Lalafo session is not initialized")
        return self.session.token

    async def _json(
        self, method: str, path: str, *, ambiguous_result: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method, path, headers=self._headers(self._token()), **kwargs
            )
        except httpx.RequestError as exc:
            error_type = (
                ManagedAdsAmbiguousResultError if ambiguous_result else ManagedAdsError
            )
            raise error_type("Lalafo request did not return a response") from exc
        if response.status_code == 401:
            raise ManagedAdsAuthenticationError("Lalafo session expired")
        if response.status_code in {403, 429}:
            raise ManagedAdsError(f"Lalafo rejected request with HTTP {response.status_code}")
        if response.status_code >= 500 and ambiguous_result:
            raise ManagedAdsAmbiguousResultError(
                f"Lalafo returned HTTP {response.status_code} after a mutating request"
            )
        if response.is_error:
            raise ManagedAdsError(f"Lalafo request failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            error_type = (
                ManagedAdsAmbiguousResultError
                if ambiguous_result else ManagedAdsContractError
            )
            raise error_type("Lalafo response is not JSON") from exc
        if not isinstance(payload, dict):
            error_type = (
                ManagedAdsAmbiguousResultError
                if ambiguous_result else ManagedAdsContractError
            )
            raise error_type("Lalafo returned an unexpected response shape")
        return payload

    async def login(self, login: str, password: str) -> ManagedSession:
        field = "email" if "@" in login else "mobile"
        response = await self._http.post(
            "/api/auth/login", headers=self._headers(),
            json={field: login, "password": password},
        )
        if response.status_code in {401, 403, 422}:
            raise ManagedAdsAuthenticationError(f"Lalafo login rejected with HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        try:
            self.session = ManagedSession(
                profile_id=int(payload["id"]), token=str(payload["token"]),
                access_token=str(payload["access_token"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManagedAdsAuthenticationError("Lalafo login response is incomplete") from exc
        self._user_hash = str(payload.get("user_hash") or self._user_hash)
        return self.session

    async def create_temp(self) -> dict[str, Any]:
        return await self._json("POST", "/api/catalog/v32/posting-ads/temp")

    async def update_temp(self, temp_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._json("PUT", f"/api/catalog/v32/posting-ads/temp/{temp_id}", json=payload)

    async def upload_image(self, temp_id: int, source_url: str) -> dict[str, Any]:
        try:
            source = await self._images.get(source_url)
            source.raise_for_status()
            filename, content_type = image_upload_metadata(
                source.content, source.headers.get("content-type", "")
            )
            upload_content = clean_jpeg_for_upload(source.content)
            filename, content_type = "apartment.jpeg", "image/jpeg"
            response = await self._http.post(
                "/api/upload/swoole-upload/v3/images/upload",
                headers={
                    **self._headers(self._token(), json=False),
                    "X-Cache-Bypass": "yes",
                },
                # Keep the same multipart field order as the current Lalafo
                # web client: the file first, followed by the draft id.
                files=[
                    ("image_file", (filename, upload_content, content_type)),
                    ("ad_id", (None, str(temp_id))),
                ],
            )
        except httpx.HTTPError as exc:
            raise ManagedAdsError("Could not download or upload a Lalafo image") from exc
        if response.status_code == 401:
            raise ManagedAdsAuthenticationError("Lalafo session expired during image upload")
        if response.status_code in {403, 429}:
            raise ManagedAdsError(
                f"Lalafo rejected image upload with HTTP {response.status_code}"
            )
        if response.is_error:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise ManagedAdsError(
                f"Lalafo image upload failed with HTTP {response.status_code}: {detail}; "
                f"sent={content_type}, name={filename}, size={len(upload_content)}, "
                f"magic={upload_content[:16].hex()}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagedAdsContractError("Image upload response is not JSON") from exc
        if not isinstance(payload, dict):
            raise ManagedAdsContractError("Image upload response is invalid")
        return payload

    async def publish_temp(self, temp_id: int) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/api/catalog/v32/posting-ads/temp/{temp_id}/publish?expand=available_campaign_types",
            json={}, ambiguous_result=True,
        )

    async def my_ad_details(self, ad_id: int) -> dict[str, Any]:
        """Read the owner-visible status used by Lalafo's own edit page."""
        return await self._json(
            "GET", f"/api/catalog/v3/feed/my-ad-details/{ad_id}"
        )

    async def wallet_balances(self) -> dict[str, Any]:
        return await self._json("GET", "/api/wallet/v3/accounts/all-balances")

    async def campaign_stats(self, ad_id: int) -> dict[str, Any]:
        return await self._json("GET", f"/api/campaign/v3/campaign-stats/get-by-ad/{ad_id}")

    async def campaign_params(self, ad_id: int) -> dict[str, Any]:
        return await self._json(
            "GET", f"/api/campaign/v3/campaign-params/ad/{ad_id}?&expand=products"
        )

    async def available_sum(self, amount: int, currency: str = "KGS") -> dict[str, Any]:
        return await self._json(
            "GET", f"/api/wallet/v3/accounts/available-sum/{amount}/currency/{currency}"
        )

    async def cancel_campaign(self, campaign_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/campaign/v3/campaigns/cancel/{campaign_id}", json={})

    async def deactivate(self, ad_ids: list[int]) -> dict[str, Any]:
        return await self._json("POST", "/api/catalog/v3/feed/deactivate-set", json={"ad_ids": ad_ids})

    async def start_campaign(
        self, ad_id: int, price_id: int, flow_id: str | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "POST", "/api/payment/v3/purchases/pay-campaign-daily",
            json={"ad_id": ad_id, "price_id": price_id, "flow_id": flow_id},
        )

    async def close(self) -> None:
        await self._http.aclose()
        await self._images.aclose()
