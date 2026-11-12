#!/usr/bin/env python3
"""Collect network metrics for Claude Code processes."""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database
from src.collectors.network_metrics import NetworkMetricsCollector
from src.utils.config import Config


async def main():
    """Run network metrics collection."""
    Config.ensure_directories()

    db = Database(str(Config.DB_PATH))
    collector = NetworkMetricsCollector(db)

    try:
        await collector.collect()
        print("Network metrics collected successfully")
    except Exception as e:
        print(f"Error collecting network metrics: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
