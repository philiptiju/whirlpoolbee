#!/usr/bin/env python3
"""Collect filesystem events."""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import Database
from src.collectors.filesystem import FilesystemCollector
from src.utils.config import Config


async def main():
    """Run filesystem collection."""
    Config.ensure_directories()

    db = Database(str(Config.DB_PATH))
    collector = FilesystemCollector(db)

    try:
        await collector.collect()
        print("Filesystem events collected successfully")
    except Exception as e:
        print(f"Error collecting filesystem events: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
