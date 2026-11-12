"""Network metrics collector for Claude Code."""

import psutil
from datetime import datetime
from typing import List, Dict, Any
from ..database import Database


class NetworkMetricsCollector:
    """Collect network metrics for Claude Code processes."""

    def __init__(self, db: Database):
        self.db = db
        self._prev_io_counters = {}

    async def collect(self):
        """Collect network metrics for all Claude processes."""
        from .process_metrics import ProcessMetricsCollector

        proc_collector = ProcessMetricsCollector(self.db)
        processes = proc_collector.find_claude_processes()

        for proc in processes:
            try:
                # Get network connections
                connections = proc.connections(kind='inet')
                connection_details = []

                for conn in connections:
                    detail = {
                        'fd': conn.fd,
                        'family': conn.family.name,
                        'type': conn.type.name,
                        'laddr': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                        'raddr': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                        'status': conn.status
                    }
                    connection_details.append(detail)

                # Log metrics
                await self.db.log_network_metrics(
                    timestamp=datetime.utcnow(),
                    pid=proc.pid,
                    num_connections=len(connections),
                    connection_details=connection_details,
                    # psutil exposes disk I/O, not per-process network byte counts.
                    bytes_sent=None,
                    bytes_recv=None
                )

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    def get_system_network_stats(self) -> Dict[str, Any]:
        """Get system-wide network statistics."""
        try:
            net_io = psutil.net_io_counters()
            return {
                'bytes_sent': net_io.bytes_sent,
                'bytes_recv': net_io.bytes_recv,
                'packets_sent': net_io.packets_sent,
                'packets_recv': net_io.packets_recv,
                'errin': net_io.errin,
                'errout': net_io.errout,
                'dropin': net_io.dropin,
                'dropout': net_io.dropout
            }
        except Exception:
            return {}

    async def get_active_connections(self) -> List[Dict[str, Any]]:
        """Get all active network connections for Claude processes."""
        from .process_metrics import ProcessMetricsCollector

        proc_collector = ProcessMetricsCollector(self.db)
        processes = proc_collector.find_claude_processes()

        all_connections = []

        for proc in processes:
            try:
                connections = proc.connections(kind='inet')
                for conn in connections:
                    all_connections.append({
                        'pid': proc.pid,
                        'process_name': proc.name(),
                        'fd': conn.fd,
                        'family': conn.family.name,
                        'type': conn.type.name,
                        'local_address': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                        'remote_address': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                        'status': conn.status
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return all_connections
