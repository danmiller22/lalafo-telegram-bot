"""XPAY checkout for an existing apartment-search request."""
from app.xpay import create_checkout as create_xpay_checkout

async def create_checkout(repository, settings, ad):
    url, _ = await create_xpay_checkout(repository.sessions, settings, wanted_ad_id=ad.id)
    return url
