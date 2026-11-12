"""Daily rollup calculator for Claude Code metrics."""

from datetime import datetime, date, timedelta
from typing import Dict, Any
from ..database import Database


class DailyRollupCollector:
    """Calculate daily rollups from detailed metrics."""

    def __init__(self, db: Database):
        self.db = db

    async def calculate_rollup(self, target_date: date = None):
        """
        Calculate daily rollup for a specific date.

        Args:
            target_date: Date to calculate rollup for. Defaults to yesterday.
        """
        if target_date is None:
            target_date = (datetime.utcnow() - timedelta(days=1)).date()

        # Get or create rollup record
        rollup = await self.db.get_or_create_daily_rollup(target_date)

        # Calculate session metrics
        session_stats = await self._calculate_session_stats(target_date)

        # Calculate token metrics
        token_stats = await self._calculate_token_stats(target_date)

        # Calculate process metrics
        process_stats = await self._calculate_process_stats(target_date)

        # Calculate network metrics
        network_stats = await self._calculate_network_stats(target_date)

        # Update rollup
        updates = {
            **session_stats,
            **token_stats,
            **process_stats,
            **network_stats
        }

        await self.db.update_daily_rollup(target_date, **updates)

    async def _calculate_session_stats(self, target_date: date) -> Dict[str, Any]:
        """Calculate session statistics for a date."""
        async with self.db.get_connection() as db:
            # Total sessions
            async with db.execute(
                """SELECT COUNT(*) as total_sessions,
                          SUM(duration_seconds) as total_duration,
                          COUNT(DISTINCT working_directory) as unique_directories
                   FROM sessions
                   WHERE DATE(start_time) = ?""", (target_date,)
            ) as cursor:
                row = await cursor.fetchone()
                total_sessions = row[0] or 0
                total_duration = row[1] or 0
                unique_directories = row[2] or 0

            # Total launches
            async with db.execute(
                """SELECT COUNT(*) FROM launches
                   WHERE DATE(timestamp) = ?""", (target_date,)
            ) as cursor:
                total_launches = (await cursor.fetchone())[0] or 0

            return {
                'total_sessions': total_sessions,
                'total_launches': total_launches,
                'total_duration_seconds': total_duration,
                'unique_directories': unique_directories
            }

    async def _calculate_token_stats(self, target_date: date) -> Dict[str, Any]:
        """Calculate token statistics for a date."""
        async with self.db.get_connection() as db:
            async with db.execute(
                """SELECT SUM(input_tokens) as input,
                          SUM(output_tokens) as output,
                          SUM(total_tokens) as total
                   FROM token_usage
                   WHERE DATE(timestamp) = ?""", (target_date,)
            ) as cursor:
                row = await cursor.fetchone()
                return {
                    'total_input_tokens': row[0] or 0,
                    'total_output_tokens': row[1] or 0,
                    'total_tokens': row[2] or 0
                }

    async def _calculate_process_stats(self, target_date: date) -> Dict[str, Any]:
        """Calculate process statistics for a date."""
        async with self.db.get_connection() as db:
            async with db.execute(
                """SELECT AVG(cpu_percent) as avg_cpu,
                          AVG(memory_rss_mb) as avg_memory,
                          MAX(memory_rss_mb) as max_memory
                   FROM process_metrics
                   WHERE DATE(timestamp) = ?""", (target_date,)
            ) as cursor:
                row = await cursor.fetchone()
                return {
                    'avg_cpu_percent': row[0],
                    'avg_memory_mb': row[1],
                    'max_memory_mb': row[2]
                }

    async def _calculate_network_stats(self, target_date: date) -> Dict[str, Any]:
        """Calculate network statistics for a date."""
        async with self.db.get_connection() as db:
            async with db.execute(
                """SELECT SUM(bytes_sent) as sent,
                          SUM(bytes_recv) as recv
                   FROM network_metrics
                   WHERE DATE(timestamp) = ?""", (target_date,)
            ) as cursor:
                row = await cursor.fetchone()
                return {
                    'total_bytes_sent': row[0] or 0,
                    'total_bytes_recv': row[1] or 0
                }

    async def backfill_rollups(self, days: int = 30):
        """Backfill rollups for the last N days."""
        today = datetime.utcnow().date()
        for i in range(days):
            target_date = today - timedelta(days=i)
            await self.calculate_rollup(target_date)
