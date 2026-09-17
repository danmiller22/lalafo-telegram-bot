from __future__ import annotations

from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.config import DEFAULT_SEARCH_URL, get_settings
from app.lalafo.chat_protocol import LalafoChatClient, LalafoGatewayError
from app.lalafo.client import LalafoClient, LalafoError


lalafo_mcp = FastMCP(
    "lalafo",
    instructions=(
        "Use these tools for Lalafo searches and listing details. Never invent "
        "missing fields and always include the returned listing URLs."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "statutory-mallissa-2danmiller-f1c1b08d.koyeb.app",
            "testserver",
            "localhost:*",
            "127.0.0.1:*",
        ],
        allowed_origins=["https://chatgpt.com", "https://platform.openai.com"],
    ),
)


def _lalafo_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"lalafo.kg", "www.lalafo.kg"}:
        raise ValueError("Only https://lalafo.kg URLs are allowed")
    return value


@lalafo_mcp.tool(
    title="Search Lalafo listings",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def search_listings(
    search_url: str = DEFAULT_SEARCH_URL,
    page: int = 1,
    seller_type: str | None = None,
) -> dict:
    """Search Lalafo using a lalafo.kg filtered search URL."""
    url = _lalafo_url(search_url)
    if page < 1 or page > 100:
        raise ValueError("page must be between 1 and 100")
    offerer = seller_type if seller_type in {"owner", "realtor"} else None
    try:
        async with LalafoClient(
            timeout=get_settings().http_timeout_seconds,
            max_retries=get_settings().http_max_retries,
            proxy_url=get_settings().lalafo_proxy_url,
        ) as client:
            result = await client.search(url, page=page, offerer=offerer)
    except LalafoError as exc:
        raise ValueError(str(exc)) from exc
    return result.model_dump(mode="json")


@lalafo_mcp.tool(
    title="Get a Lalafo listing",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_listing(listing_url: str) -> dict:
    """Return the current details for one lalafo.kg listing URL."""
    url = _lalafo_url(listing_url)
    try:
        async with LalafoClient(
            timeout=get_settings().http_timeout_seconds,
            max_retries=get_settings().http_max_retries,
            proxy_url=get_settings().lalafo_proxy_url,
        ) as client:
            result = await client.detail(url)
    except LalafoError as exc:
        raise ValueError(str(exc)) from exc
    return result.model_dump(mode="json")


@lalafo_mcp.tool(
    title="Check Lalafo cloud login",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def account_status() -> dict[str, str | bool]:
    """Check whether the cloud service can authenticate to the configured Lalafo account."""
    settings = get_settings()
    try:
        login, password = settings.require_lalafo_auto_reply_credentials()
    except RuntimeError:
        return {"configured": False, "authenticated": False, "state": "credentials_missing"}
    client = LalafoChatClient(login=login, password=password, fingerprint="codex-cloud-mcp")
    try:
        await client.get_session()
        return {"configured": True, "authenticated": True, "state": "ready"}
    except LalafoGatewayError as exc:
        return {"configured": True, "authenticated": False, "state": exc.kind}
    finally:
        await client.close()


lalafo_mcp_app = lalafo_mcp.streamable_http_app()
