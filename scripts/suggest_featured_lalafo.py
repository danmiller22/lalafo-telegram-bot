from __future__ import annotations

import asyncio
import logging


async def run() -> int:
    """Keep the retired shortlist entrypoint harmless for old cloud schedules."""
    logging.basicConfig(level=logging.INFO)
    logging.info(
        "Automatic featured suggestions are disabled; send an explicit Lalafo "
        "link to the manual advertising bot"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
