"""Session logger for Claude Code."""

import os
import uuid
from datetime import datetime
from ..database import Database


class SessionLogger:
    """Log Claude Code session events."""

    def __init__(self, db: Database):
        self.db = db

    async def start_session(
        self,
        working_directory: str,
        command_args: str = None,
        model_name: str = None
    ) -> str:
        """
        Start a new session.

        Returns:
            Session ID
        """
        session_id = str(uuid.uuid4())

        await self.db.create_session(
            session_id=session_id,
            start_time=datetime.utcnow(),
            working_directory=working_directory,
            command_args=command_args,
            model_name=model_name
        )

        # Also log as launch
        await self.db.log_launch(
            session_id=session_id,
            timestamp=datetime.utcnow(),
            working_directory=working_directory,
            command_line=command_args or 'claude',
            shell_pid=os.getppid()
        )

        return session_id

    async def end_session(self, session_id: str, exit_code: int = 0):
        """End a session."""
        await self.db.end_session(
            session_id=session_id,
            end_time=datetime.utcnow(),
            exit_code=exit_code
        )

    async def log_token_usage(
        self,
        session_id: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int
    ):
        """Log token usage for a session."""
        from ..models import estimate_cost

        total_tokens = input_tokens + output_tokens
        cost = estimate_cost(model_name, input_tokens, output_tokens)

        await self.db.log_token_usage(
            timestamp=datetime.utcnow(),
            session_id=session_id,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost
        )
