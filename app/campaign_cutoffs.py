"""Stop the explicit prepaid one-day campaigns without polling the ad service."""
import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
cutoff_state = {"state": "pending"}
PLAN_PATH = Path(__file__).resolve().parents[1] / "tools" / "lalafo_campaigns_20261014.json"


async def _run_plan(path, state):
    if not path.exists():
        return
    from tools.stop_lalafo_campaigns import run
    plan = json.loads(path.read_text())
    state.update(state="scheduled", stop_at=plan["stop_at"], campaigns=len(plan["campaigns"]))
    try:
        await asyncio.wait_for(run(plan, verify_only=True), timeout=120)
        state.update(access="verified")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.update(access="unavailable", error=type(exc).__name__)
    while True:
        try:
            # The existing runner sleeps until the cutoff, then authenticates
            # and verifies ownership of these exact campaign IDs before stopping.
            await run(plan)
            state.update(state="completed")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.update(state="retrying", error=type(exc).__name__)
            logger.warning("Requested Lalafo cutoff will retry: %s", type(exc).__name__)
            await asyncio.sleep(60)


async def run_requested_campaign_cutoff():
    additional = PLAN_PATH.with_name("lalafo_campaigns_20261015_three.json")
    jobs = [_run_plan(PLAN_PATH, cutoff_state)]
    if additional.exists():
        state = cutoff_state.setdefault("requested_three_20261015", {})
        jobs.append(_run_plan(additional, state))
    await asyncio.gather(*jobs)
