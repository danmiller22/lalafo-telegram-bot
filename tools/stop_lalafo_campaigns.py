"""Stop only a pre-approved, owned set of one-day campaigns; never spend money."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from app.config import get_settings
from app.lalafo.managed_ads import LalafoManagedAdsClient
from scripts.select_lalafo_proxy import find_working_proxies


def now_utc():
    return datetime.now(timezone.utc)


def owned_campaign(details, *, owner_id: int, campaign_id: int):
    if owner_id not in details.get('user_ids', [details.get('user_id')]):
        raise RuntimeError('Campaign account ownership mismatch')
    return next((c for c in details.get('campaign', []) if int(c['id']) == campaign_id), None)


async def stop_authorized(client, plan, *, now, verify_only=False):
    deadline = datetime.fromisoformat(plan['stop_at'])
    if not verify_only and now < deadline:
        raise RuntimeError('Refusing to stop promotion before its authorized deadline')
    if client.session.profile_id != plan['owner_profile_id']:
        raise RuntimeError('Authenticated account does not match the promotion owner')
    stopped = checked = already_finished = 0
    for row in plan['campaigns']:
        details = await client.my_ad_details(row['ad_id'])
        if int(details['id']) != row['ad_id']:
            raise RuntimeError('Advertisement identity mismatch')
        campaign = owned_campaign(details, owner_id=plan['owner_profile_id'], campaign_id=row['campaign_id'])
        checked += 1
        if verify_only:
            if campaign is None:
                raise RuntimeError('Expected promotion was not found')
            continue
        if campaign is None or campaign.get('status') != 'started':
            already_finished += 1
            continue
        await client.cancel_campaign(str(row['campaign_id']))
        refreshed = await client.my_ad_details(row['ad_id'])
        current = owned_campaign(refreshed, owner_id=plan['owner_profile_id'], campaign_id=row['campaign_id'])
        if current is not None and current.get('status') == 'started':
            raise RuntimeError('Campaign cancellation was not confirmed')
        stopped += 1
    return {'checked': checked, 'stopped': stopped, 'already_finished': already_finished, 'verify_only': verify_only}


async def authenticated_client(plan):
    settings = get_settings()
    login, password = settings.require_lalafo_auto_reply_credentials()
    routes = await find_working_proxies()
    # Keep direct access as a final fallback. No proxy URL or credential is logged.
    last_error = None
    for proxy in [*routes, None]:
        client = LalafoManagedAdsClient(timeout=15)
        if proxy:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url='https://lalafo.kg', proxy=proxy, timeout=15, follow_redirects=True)
        try:
            await client.login(login, password)
            if client.session.profile_id != plan['owner_profile_id']:
                raise RuntimeError('Configured account differs from authorized campaign owner')
            return client
        except Exception as exc:
            last_error = type(exc).__name__
            await client.close()
    raise RuntimeError(f'No authenticated Lalafo route: {last_error}')


async def run(plan, *, verify_only=False):
    deadline = datetime.fromisoformat(plan['stop_at'])
    if not verify_only and now_utc() > deadline + timedelta(days=2):
        print('Expired one-off plan; no account changes')
        return
    client = await authenticated_client(plan)
    try:
        if verify_only:
            print(json.dumps(await stop_authorized(client, plan, now=now_utc(), verify_only=True)))
            return
        # The scheduled runner starts early so queue/setup delays do not cause
        # a second daily payment. Never stop the paid promotion early on dispatch.
        while now_utc() < deadline:
            await asyncio.sleep(min(20, (deadline - now_utc()).total_seconds()))
        for attempt in range(3):
            try:
                print(json.dumps(await stop_authorized(client, plan, now=now_utc())))
                return
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(10)
    finally:
        await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', default='tools/lalafo_campaigns_20261012.json')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(json.loads(Path(args.plan).read_text()), verify_only=args.verify_only))
