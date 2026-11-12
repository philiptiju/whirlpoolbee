"""Tests for data parsers."""

import pytest
from datetime import datetime
from pathlib import Path
import tempfile

# Add parent to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.parsers.shell_history import ShellHistoryParser


class TestShellHistoryParser:
    """Test shell history parsing."""

    def test_parse_zsh_history(self, tmp_path):
        """Test parsing zsh history format."""
        history_file = tmp_path / ".zsh_history"
        history_file.write_text(
            ": 1234567890:0;claude --help\n"
            ": 1234567900:0;ls -la\n"
            ": 1234567910:0;claude code\n"
        )

        parser = ShellHistoryParser()
        commands = parser.parse_file(str(history_file))

        assert len(commands) == 2
        assert "claude --help" in commands[0]['command']
        assert "claude code" in commands[1]['command']
        assert commands[0]['shell'] == 'zsh'

    def test_parse_bash_history(self, tmp_path):
        """Test parsing bash history format."""
        history_file = tmp_path / ".bash_history"
        history_file.write_text(
            "#1234567890\n"
            "claude --help\n"
            "#1234567900\n"
            "ls -la\n"
            "#1234567910\n"
            "claude code\n"
        )

        parser = ShellHistoryParser()
        commands = parser.parse_file(str(history_file))

        assert len(commands) == 2
        assert "claude --help" in commands[0]['command']
        assert commands[0]['shell'] == 'bash'

    def test_extract_claude_args(self):
        """Test extracting arguments from claude command."""
        parser = ShellHistoryParser()

        args = parser.extract_claude_args("claude --help --verbose")
        assert '--help' in args['flags']
        assert '--verbose' in args['flags']


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
