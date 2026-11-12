#!/usr/bin/env python3
"""Collect process metrics for Claude Code processes."""

import sys
import asyncio
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database
from src.collectors.process_metrics import ProcessMetricsCollector
from src.utils.config import Config


async def main():
    """Run process metrics collection."""
    Config.ensure_directories()

    db = Database(str(Config.DB_PATH))
    collector = ProcessMetricsCollector(db)

    try:
        await collector.collect()
        print("Process metrics collected successfully")
    except Exception as e:
        print(f"Error collecting process metrics: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
