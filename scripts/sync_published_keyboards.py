from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run() -> int:
    logger.info("Historical keyboard synchronization is disabled; new cards only")
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run()))
    except SystemExit:
        raise
    except Exception as exc:
        logging.basicConfig(level=logging.INFO)
        logger.exception(
            "Legacy keyboard maintenance failed safely: %s", type(exc).__name__
        )
        raise SystemExit(0)


if __name__ == "__main__":
    main()
