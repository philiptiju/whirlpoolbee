"""Data models for WhirlpoolBee."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


class EventType(Enum):
    """Filesystem event types."""
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"


class ProcessStatus(Enum):
    """Process status values."""
    RUNNING = "running"
    SLEEPING = "sleeping"
    ZOMBIE = "zombie"
    STOPPED = "stopped"


@dataclass
class Session:
    """Claude Code session."""
    session_id: str
    start_time: datetime
    working_directory: str
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    command_args: Optional[str] = None
    model_name: Optional[str] = None
    exit_code: Optional[int] = None
    id: Optional[int] = None


@dataclass
class Launch:
    """Claude launch event."""
    timestamp: datetime
    working_directory: str
    command_line: str
    session_id: Optional[str] = None
    shell_pid: Optional[int] = None
    id: Optional[int] = None


@dataclass
class ProcessMetrics:
    """Process performance metrics."""
    timestamp: datetime
    pid: int
    session_id: Optional[str] = None
    cpu_percent: Optional[float] = None
    memory_rss_mb: Optional[float] = None
    memory_vms_mb: Optional[float] = None
    memory_percent: Optional[float] = None
    num_threads: Optional[int] = None
    num_fds: Optional[int] = None
    process_status: Optional[str] = None
    id: Optional[int] = None


@dataclass
class NetworkMetrics:
    """Network usage metrics."""
    timestamp: datetime
    pid: int
    session_id: Optional[str] = None
    bytes_sent: Optional[int] = None
    bytes_recv: Optional[int] = None
    packets_sent: Optional[int] = None
    packets_recv: Optional[int] = None
    num_connections: Optional[int] = None
    connection_details: Optional[List[Dict[str, Any]]] = None
    id: Optional[int] = None


@dataclass
class FilesystemEvent:
    """Filesystem event."""
    timestamp: datetime
    file_path: str
    event_type: str
    session_id: Optional[str] = None
    file_size_bytes: Optional[int] = None
    id: Optional[int] = None


@dataclass
class TokenUsage:
    """Token usage record."""
    timestamp: datetime
    session_id: str
    model_name: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    id: Optional[int] = None


@dataclass
class DailyRollup:
    """Daily aggregated statistics."""
    date: str
    total_sessions: int = 0
    total_launches: int = 0
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_seconds: int = 0
    avg_cpu_percent: Optional[float] = None
    avg_memory_mb: Optional[float] = None
    max_memory_mb: Optional[float] = None
    total_bytes_sent: int = 0
    total_bytes_recv: int = 0
    unique_directories: int = 0
    id: Optional[int] = None


# Model pricing (USD per million tokens)
MODEL_PRICING = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20240620": {"input": 3.00, "output": 15.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
}


def estimate_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a model and token counts."""
    pricing = MODEL_PRICING.get(model_name)
    if not pricing:
        # Try to match partial model name
        for key, value in MODEL_PRICING.items():
            if key in model_name or model_name in key:
                pricing = value
                break

    if not pricing:
        return 0.0

    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost
