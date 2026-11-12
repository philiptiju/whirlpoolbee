"""Auto-discovery of Claude Code data sources."""

import sqlite3
from datetime import datetime
from typing import Dict, Any
from ..utils.config import Config
from ..database import Database


class DataDiscovery:
    """Discover Claude Code data from local filesystem."""

    def __init__(self, db: Database):
        self.db = db

    async def discover_all(self) -> Dict[str, Any]:
        """
        Discover all Claude data sources.

        Returns:
            Summary of discovered data
        """
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "sources": {},
            "total_records": 0,
            "errors": []
        }

        # Discover config files
        config_results = await self.discover_configs()
        results["sources"]["configs"] = config_results
        results["total_records"] += config_results["count"]

        # Discover log files
        log_results = await self.discover_logs()
        results["sources"]["logs"] = log_results
        results["total_records"] += log_results["count"]

        # Discover SQLite databases
        db_results = await self.discover_databases()
        results["sources"]["databases"] = db_results
        results["total_records"] += db_results["count"]

        # Discover shell history
        history_results = await self.discover_shell_history()
        results["sources"]["shell_history"] = history_results
        results["total_records"] += history_results["count"]

        return results

    async def discover_configs(self) -> Dict[str, Any]:
        """Discover Claude configuration files."""
        found = []
        seen = set()
        for location in Config.CLAUDE_LOCATIONS:
            if not location.exists():
                continue

            # Look for common config file names
            for pattern in ["config.json", "settings.json", "*.json"]:
                for config_file in location.glob(f"**/{pattern}"):
                    resolved = config_file.resolve()
                    if config_file.is_file() and resolved not in seen:
                        seen.add(resolved)
                        found.append(str(config_file))
                        await self.db.log_discovery(
                            timestamp=datetime.utcnow(),
                            source_type="config",
                            source_path=str(config_file),
                            records_found=1,
                            successfully_parsed=True
                        )

        return {"count": len(found), "files": found}

    async def discover_logs(self) -> Dict[str, Any]:
        """Discover Claude log files."""
        found = []
        seen = set()
        for location in Config.CLAUDE_LOCATIONS:
            if not location.exists():
                continue

            # Look for log files
            for pattern in ["*.log", "logs/*.log"]:
                for log_file in location.glob(f"**/{pattern}"):
                    resolved = log_file.resolve()
                    if log_file.is_file() and resolved not in seen:
                        seen.add(resolved)
                        found.append(str(log_file))
                        await self.db.log_discovery(
                            timestamp=datetime.utcnow(),
                            source_type="log",
                            source_path=str(log_file),
                            records_found=1,
                            successfully_parsed=True
                        )

        return {"count": len(found), "files": found}

    async def discover_databases(self) -> Dict[str, Any]:
        """Discover SQLite databases related to Claude."""
        found = []
        for location in Config.CLAUDE_LOCATIONS:
            if not location.exists():
                continue

            for db_file in location.glob("**/*.db"):
                if db_file.is_file():
                    try:
                        # Verify it's a valid SQLite database
                        conn = sqlite3.connect(str(db_file))
                        cursor = conn.cursor()
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        tables = cursor.fetchall()
                        conn.close()

                        if tables:
                            found.append({
                                "path": str(db_file),
                                "tables": [t[0] for t in tables]
                            })

                            await self.db.log_discovery(
                                timestamp=datetime.utcnow(),
                                source_type="sqlite",
                                source_path=str(db_file),
                                records_found=len(tables),
                                successfully_parsed=True
                            )
                    except sqlite3.Error:
                        pass  # Not a valid SQLite file

        return {"count": len(found), "databases": found}

    async def discover_shell_history(self) -> Dict[str, Any]:
        """Discover Claude usage from shell history."""
        found = []
        total_commands = 0

        for history_file in Config.SHELL_HISTORY_FILES:
            if not history_file.exists():
                continue

            try:
                with open(history_file, 'r', errors='ignore') as f:
                    lines = f.readlines()

                # Count Claude-related commands
                claude_commands = [
                    line for line in lines
                    if 'claude' in line.lower() and not line.strip().startswith('#')
                ]

                if claude_commands:
                    found.append({
                        "file": str(history_file),
                        "total_lines": len(lines),
                        "claude_commands": len(claude_commands)
                    })
                    total_commands += len(claude_commands)

                    await self.db.log_discovery(
                        timestamp=datetime.utcnow(),
                        source_type="history",
                        source_path=str(history_file),
                        records_found=len(claude_commands),
                        successfully_parsed=True
                    )
            except Exception as e:
                await self.db.log_discovery(
                    timestamp=datetime.utcnow(),
                    source_type="history",
                    source_path=str(history_file),
                    records_found=0,
                    successfully_parsed=False,
                    error_message=str(e)
                )

        return {
            "count": total_commands,
            "files": found
        }

    async def get_discovery_summary(self) -> Dict[str, Any]:
        """Get summary of discovered data sources."""
        async with await self.db.get_connection() as conn:
            conn.row_factory = sqlite3.Row

            # Get counts by source type
            async with conn.execute(
                """SELECT source_type, COUNT(*) as count,
                          SUM(records_found) as total_records
                   FROM discovery_log
                   GROUP BY source_type
                   ORDER BY count DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                by_type = [dict(row) for row in rows]

            # Get recent discoveries
            async with conn.execute(
                """SELECT * FROM discovery_log
                   ORDER BY timestamp DESC
                   LIMIT 20"""
            ) as cursor:
                rows = await cursor.fetchall()
                recent = [dict(row) for row in rows]

            return {
                "by_type": by_type,
                "recent": recent
            }
