"""Secret redaction utilities for WhirlpoolBee."""

import re
from typing import Any, Dict, List, Tuple


# Common secret patterns
SECRET_PATTERNS = [
    # API Keys
    (r'(api[_-]?key|apikey)\s*[=:]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', 'API_KEY'),
    (r'sk-[a-zA-Z0-9]{20,}', 'ANTHROPIC_KEY'),
    (r'ghp_[a-zA-Z0-9]{36}', 'GITHUB_TOKEN'),
    (r'gho_[a-zA-Z0-9]{36}', 'GITHUB_OAUTH'),

    # AWS
    (r'AKIA[0-9A-Z]{16}', 'AWS_ACCESS_KEY'),
    (r'aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*["\']?([a-zA-Z0-9/+=]{40})["\']?', 'AWS_SECRET'),

    # Tokens
    (r'(bearer|token)\s+([a-zA-Z0-9_\-\.]{20,})', 'TOKEN'),
    (r'(password|passwd|pwd)\s*[=:]\s*["\']?([^\s\'"]{8,})["\']?', 'PASSWORD'),

    # Private keys
    (r'-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----', 'PRIVATE_KEY'),

    # JWTs
    (r'eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*', 'JWT'),

    # Generic credentials
    (r'(client[_-]?secret|consumer[_-]?secret)\s*[=:]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', 'CLIENT_SECRET'),
]


def redact_secrets(text: str, patterns: List[Tuple[str, str]] = None) -> str:
    """
    Redact secrets from text using regex patterns.

    Args:
        text: Text to redact
        patterns: List of (regex, label) tuples. Uses defaults if None.

    Returns:
        Text with secrets redacted
    """
    if patterns is None:
        patterns = SECRET_PATTERNS

    redacted = text
    for pattern, label in patterns:
        redacted = re.sub(
            pattern,
            f'[REDACTED_{label}]',
            redacted,
            flags=re.IGNORECASE | re.DOTALL
        )

    return redacted


def contains_secrets(text: str, patterns: List[Tuple[str, str]] = None) -> bool:
    """
    Check if text contains potential secrets.

    Args:
        text: Text to check
        patterns: List of (regex, label) tuples. Uses defaults if None.

    Returns:
        True if potential secrets found
    """
    if patterns is None:
        patterns = SECRET_PATTERNS

    for pattern, _ in patterns:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return True

    return False


def get_secret_locations(text: str, patterns: List[Tuple[str, str]] = None) -> List[Dict[str, Any]]:
    """
    Find locations of secrets in text.

    Args:
        text: Text to search
        patterns: List of (regex, label) tuples. Uses defaults if None.

    Returns:
        List of dicts with 'start', 'end', 'label', 'match'
    """
    if patterns is None:
        patterns = SECRET_PATTERNS

    locations = []
    for pattern, label in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            locations.append({
                'start': match.start(),
                'end': match.end(),
                'label': label,
                'match': match.group()
            })

    return sorted(locations, key=lambda x: x['start'])
