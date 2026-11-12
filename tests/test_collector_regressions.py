import os
import sqlite3
from datetime import datetime, date
from unittest.mock import AsyncMock, Mock

import pytest

from src.collectors.network_metrics import NetworkMetricsCollector
from src.collectors.process_metrics import ProcessMetricsCollector


@pytest.mark.asyncio
async def test_network_collector_does_not_report_disk_io(database, monkeypatch):
    proc = Mock(pid=123)
    proc.connections.return_value = []
    proc.io_counters.return_value = Mock(write_bytes=999, read_bytes=888)
    monkeypatch.setattr(ProcessMetricsCollector, "find_claude_processes", lambda self: [proc])
    await NetworkMetricsCollector(database).collect()
    proc.io_counters.assert_not_called()
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT pid, num_connections, bytes_sent, bytes_recv FROM network_metrics"
        ).fetchone() == (123, 0, None, None)


@pytest.mark.asyncio
async def test_rollup_backfill_uses_utc_day(database, monkeypatch):
    from src.collectors import daily_rollup

    clock = Mock()
    clock.utcnow.return_value = datetime(2026, 9, 30, 20, 0)
    monkeypatch.setattr(daily_rollup, "datetime", clock)
    collector = daily_rollup.DailyRollupCollector(database)
    collector.calculate_rollup = AsyncMock()
    await collector.backfill_rollups(days=2)
    assert [c.args[0] for c in collector.calculate_rollup.await_args_list] == [
        date(2026, 9, 30), date(2026, 9, 29)
    ]


@pytest.mark.asyncio
async def test_daily_rollup_aggregates_utc_boundaries(database):
    from src.collectors.daily_rollup import DailyRollupCollector

    with sqlite3.connect(database.db_path) as conn:
        for i, timestamp in enumerate(["2026-10-01T00:15:00+05:30", "2026-10-01T06:00:00+05:30"]):
            conn.execute(
                """INSERT INTO sessions (session_id, machine_id, start_time, duration_seconds)
                   VALUES (?, 'mine', ?, 100)""", (str(i), timestamp),
            )
    collector = DailyRollupCollector(database)
    await collector.calculate_rollup(date(2026, 9, 30))
    await collector.calculate_rollup(date(2026, 10, 1))
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute(
            "SELECT total_sessions, total_duration_seconds FROM daily_rollups ORDER BY date"
        ).fetchall() == [(1, 100), (1, 100)]


@pytest.mark.asyncio
async def test_discovery_deduplicates_patterns_and_locations(database, tmp_path, monkeypatch):
    from src.collectors.discovery import DataDiscovery
    from src.utils.config import Config

    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "config.json").write_text("{}")
    (logs / "settings.json").write_text("{}")
    (logs / "app.log").write_text("log")
    monkeypatch.setattr(Config, "CLAUDE_LOCATIONS", [tmp_path, logs])
    discovery = DataDiscovery(database)
    assert (await discovery.discover_configs())["count"] == 2
    assert (await discovery.discover_logs())["count"] == 1
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_log").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_filesystem_baseline_does_not_fabricate_reads(tmp_path):
    from src.collectors.filesystem import FilesystemCollector

    db = Mock(log_filesystem_event=AsyncMock())
    collector = FilesystemCollector(db)
    file = tmp_path / "example.py"
    file.write_text("original")
    await collector.scan_directory(str(tmp_path))
    await collector.scan_directory(str(tmp_path))
    db.log_filesystem_event.assert_not_awaited()
    mtime = file.stat().st_mtime + 2
    file.write_text("updated")
    os.utime(file, (mtime, mtime))
    await collector.scan_directory(str(tmp_path))
    db.log_filesystem_event.assert_awaited_once()
    assert db.log_filesystem_event.await_args.kwargs["event_type"] == "write"
