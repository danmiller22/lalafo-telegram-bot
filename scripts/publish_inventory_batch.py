from __future__ import annotations

import asyncio
import logging
from scripts.inventory_cycle import run as run_inventory_cycle


logger = logging.getLogger(__name__)


async def run() -> int:
    """Discover when due and publish no more than one randomly scheduled card."""
    return await run_inventory_cycle()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
