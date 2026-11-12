"""Filesystem event collector for Claude Code."""

import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
from ..database import Database


class FilesystemCollector:
    """Collect filesystem events from Claude sessions."""

    def __init__(self, db: Database):
        self.db = db
        self._tracked_files = {}

    async def collect(self, scan_directories: List[str] = None):
        """
        Scan directories for file changes.

        Args:
            scan_directories: List of directories to scan.
                            If None, uses recent session directories.
        """
        if scan_directories is None:
            # Get directories from recent sessions
            sessions = await self.db.get_recent_sessions(limit=50)
            scan_directories = [
                s['working_directory'] for s in sessions
                if s['working_directory']
            ]

        # Deduplicate directories
        unique_dirs = list(set(scan_directories))

        for directory in unique_dirs:
            if not os.path.exists(directory):
                continue

            await self.scan_directory(directory)

    async def scan_directory(self, directory: str, max_depth: int = 3):
        """
        Scan a directory for recently modified files.

        Args:
            directory: Directory to scan
            max_depth: Maximum depth to recurse
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=24)

        try:
            for root, dirs, files in os.walk(directory):
                # Calculate depth
                depth = root[len(directory):].count(os.sep)
                if depth >= max_depth:
                    dirs[:] = []  # Don't recurse deeper
                    continue

                # Skip hidden and common directories
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.')
                    and d not in ['node_modules', 'venv', '__pycache__', 'dist', 'build']
                ]

                for filename in files:
                    # Skip hidden files and common binary extensions
                    if filename.startswith('.'):
                        continue

                    file_path = Path(root) / filename
                    if not file_path.exists():
                        continue

                    # Skip binary files
                    if file_path.suffix in [
                        '.pyc', '.so', '.dylib', '.dll', '.exe',
                        '.jpg', '.png', '.gif', '.pdf', '.zip'
                    ]:
                        continue

                    try:
                        stat = file_path.stat()
                        mtime = datetime.utcfromtimestamp(stat.st_mtime)

                        # Only track recently modified files
                        if mtime < cutoff_time:
                            continue

                        # Determine event type
                        file_key = str(file_path)
                        if file_key in self._tracked_files:
                            prev_mtime = self._tracked_files[file_key]
                            if mtime > prev_mtime:
                                event_type = 'write'
                            else:
                                continue  # No change
                        else:
                            # A first observation establishes a baseline, not a read.
                            self._tracked_files[file_key] = mtime
                            continue

                        # Update tracking
                        self._tracked_files[file_key] = mtime

                        # Log event
                        await self.db.log_filesystem_event(
                            timestamp=mtime,
                            file_path=file_key,
                            event_type=event_type,
                            file_size_bytes=stat.st_size
                        )

                    except (OSError, PermissionError):
                        continue

        except (OSError, PermissionError):
            pass

    async def get_recent_files(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recently accessed files."""
        async with await self.db.get_connection() as db:
            db.row_factory = lambda cursor, row: dict(
                zip([col[0] for col in cursor.description], row)
            )

            async with db.execute(
                """SELECT file_path, event_type, timestamp, file_size_bytes
                   FROM filesystem_events
                   ORDER BY timestamp DESC
                   LIMIT ?""", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return rows

    async def get_file_stats(self) -> Dict[str, Any]:
        """Get filesystem statistics."""
        async with await self.db.get_connection() as db:
            # Total events
            async with db.execute(
                "SELECT COUNT(*) FROM filesystem_events"
            ) as cursor:
                total_events = (await cursor.fetchone())[0]

            # Events by type
            async with db.execute(
                """SELECT event_type, COUNT(*) as count
                   FROM filesystem_events
                   GROUP BY event_type"""
            ) as cursor:
                rows = await cursor.fetchall()
                by_type = dict(rows)

            # Total size
            async with db.execute(
                "SELECT SUM(file_size_bytes) FROM filesystem_events"
            ) as cursor:
                total_bytes = (await cursor.fetchone())[0] or 0

            return {
                'total_events': total_events,
                'by_type': by_type,
                'total_bytes': total_bytes
            }
