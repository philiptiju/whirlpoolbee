"""Process metrics collector for Claude Code."""

import psutil
from datetime import datetime
from typing import List
from ..database import Database


class ProcessMetricsCollector:
    """Collect process metrics for Claude Code processes."""

    def __init__(self, db: Database):
        self.db = db

    async def collect(self):
        """Collect process metrics for all Claude processes."""
        processes = self.find_claude_processes()

        for proc in processes:
            try:
                # Get process info
                with proc.oneshot():
                    cpu_percent = proc.cpu_percent(interval=0.1)
                    mem_info = proc.memory_info()
                    mem_percent = proc.memory_percent()
                    num_threads = proc.num_threads()

                    try:
                        num_fds = proc.num_fds()
                    except (AttributeError, psutil.AccessDenied):
                        num_fds = None

                    status = proc.status()

                # Convert memory to MB
                memory_rss_mb = mem_info.rss / (1024 * 1024)
                memory_vms_mb = mem_info.vms / (1024 * 1024)

                # Log metrics
                await self.db.log_process_metrics(
                    timestamp=datetime.utcnow(),
                    pid=proc.pid,
                    cpu_percent=cpu_percent,
                    memory_rss_mb=memory_rss_mb,
                    memory_vms_mb=memory_vms_mb,
                    memory_percent=mem_percent,
                    num_threads=num_threads,
                    num_fds=num_fds,
                    process_status=status
                )

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    def find_claude_processes(self) -> List[psutil.Process]:
        """Find all running Claude processes."""
        claude_processes = []

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # Check process name
                name = proc.info['name']
                if name and 'claude' in name.lower():
                    claude_processes.append(proc)
                    continue

                # Check command line
                cmdline = proc.info['cmdline']
                if cmdline:
                    cmdline_str = ' '.join(cmdline).lower()
                    if 'claude' in cmdline_str:
                        claude_processes.append(proc)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return claude_processes

    async def get_current_processes(self) -> List[dict]:
        """Get current Claude processes with their info."""
        processes = self.find_claude_processes()
        result = []

        for proc in processes:
            try:
                with proc.oneshot():
                    info = {
                        'pid': proc.pid,
                        'name': proc.name(),
                        'status': proc.status(),
                        'cpu_percent': proc.cpu_percent(interval=0.1),
                        'memory_mb': proc.memory_info().rss / (1024 * 1024),
                        'num_threads': proc.num_threads(),
                        'create_time': datetime.utcfromtimestamp(proc.create_time()).isoformat(),
                    }

                    try:
                        info['cwd'] = proc.cwd()
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        info['cwd'] = None

                    result.append(info)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return result
