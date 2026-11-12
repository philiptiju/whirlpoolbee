"""Shell history parser for Claude Code usage."""

import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any


class ShellHistoryParser:
    """Parse shell history files for Claude usage."""

    def parse_file(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Parse shell history file for Claude commands.

        Returns:
            List of Claude command entries
        """
        path = Path(file_path)
        if not path.exists():
            return []

        shell_type = self._detect_shell_type(path)

        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            return []

        if shell_type == 'zsh':
            return self._parse_zsh_history(lines)
        elif shell_type == 'bash':
            return self._parse_bash_history(lines)
        else:
            return self._parse_generic_history(lines)

    def _detect_shell_type(self, path: Path) -> str:
        """Detect shell type from history file path."""
        name = path.name.lower()
        if 'zsh' in name:
            return 'zsh'
        elif 'bash' in name:
            return 'bash'
        else:
            return 'generic'

    def _parse_zsh_history(self, lines: List[str]) -> List[Dict[str, Any]]:
        """
        Parse zsh history format.

        Format: : <timestamp>:<duration>;<command>
        """
        commands = []

        for line in lines:
            line = line.strip()
            if not line or not line.startswith(':'):
                continue

            # Match zsh history format
            match = re.match(r'^:\s*(\d+):(\d+);(.*)$', line)
            if match:
                timestamp = int(match.group(1))
                command = match.group(3)

                if 'claude' in command.lower():
                    commands.append({
                        'timestamp': datetime.fromtimestamp(timestamp),
                        'command': command,
                        'shell': 'zsh'
                    })

        return commands

    def _parse_bash_history(self, lines: List[str]) -> List[Dict[str, Any]]:
        """
        Parse bash history format.

        Format: #<timestamp>\n<command>
        """
        commands = []
        current_timestamp = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Check for timestamp
            if line.startswith('#'):
                try:
                    current_timestamp = int(line[1:])
                except ValueError:
                    pass
                continue

            # Check if command contains 'claude'
            if 'claude' in line.lower():
                timestamp = (
                    datetime.fromtimestamp(current_timestamp)
                    if current_timestamp
                    else None
                )

                commands.append({
                    'timestamp': timestamp,
                    'command': line,
                    'shell': 'bash'
                })

        return commands

    def _parse_generic_history(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Parse generic history format (just commands)."""
        commands = []

        for line in lines:
            line = line.strip()
            if 'claude' in line.lower() and not line.startswith('#'):
                commands.append({
                    'timestamp': None,
                    'command': line,
                    'shell': 'unknown'
                })

        return commands

    def extract_claude_args(self, command: str) -> Dict[str, Any]:
        """Extract arguments from a Claude command."""
        # Remove 'claude' and split by spaces
        parts = command.strip().split()
        if not parts or 'claude' not in parts[0].lower():
            return {}

        args = {
            'raw_command': command,
            'flags': [],
            'positional': []
        }

        # Skip the 'claude' part
        i = 1
        while i < len(parts):
            part = parts[i]

            if part.startswith('-'):
                args['flags'].append(part)
                # Check if flag has value
                if i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                    i += 1
            else:
                args['positional'].append(part)

            i += 1

        return args
