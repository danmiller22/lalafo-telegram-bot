"""Stop the explicit prepaid one-day campaigns without polling the ad service."""
import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
cutoff_state = {"state": "pending"}
PLAN_PATH = Path(__file__).resolve().parents[1] / "tools" / "lalafo_campaigns_20261014.json"


async def run_requested_campaign_cutoff():
    if not PLAN_PATH.exists():
        return
    from tools.stop_lalafo_campaigns import run
    plan = json.loads(PLAN_PATH.read_text())
    cutoff_state.update(state="scheduled", stop_at=plan["stop_at"], campaigns=len(plan["campaigns"]))
    try:
        await asyncio.wait_for(run(plan, verify_only=True), timeout=120)
        cutoff_state.update(access="verified")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        cutoff_state.update(access="unavailable", error=type(exc).__name__)
    while True:
        try:
            # The existing runner sleeps until the cutoff, then authenticates
            # and verifies ownership of these exact campaign IDs before stopping.
            await run(plan)
            cutoff_state.update(state="completed")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cutoff_state.update(state="retrying", error=type(exc).__name__)
            logger.warning("Requested Lalafo cutoff will retry: %s", type(exc).__name__)
            await asyncio.sleep(60)
