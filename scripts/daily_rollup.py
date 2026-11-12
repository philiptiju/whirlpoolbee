#!/usr/bin/env python3
"""Calculate daily rollups."""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database
from src.collectors.daily_rollup import DailyRollupCollector
from src.utils.config import Config


async def main():
    """Run daily rollup calculation."""
    Config.ensure_directories()

    db = Database(str(Config.DB_PATH))
    collector = DailyRollupCollector(db)

    try:
        await collector.calculate_rollup()
        print("Daily rollup calculated successfully")
    except Exception as e:
        print(f"Error calculating daily rollup: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
