#!/usr/bin/env python3
"""Sign src/web/static/client_agent.py for the auto-updater's Ed25519
signature check (see check_for_update() in client_agent.py).

Run this any time client_agent.py changes, BEFORE building/deploying the
Docker image — the signature file (client_agent.py.sig) must always match
whatever content is actually served at /static/client_agent.py, or every
agent in the fleet will reject the "update" (fail-closed by design).

Usage:
    python scripts/sign_agent.py --key /path/to/agent_signing_private_key.pem

The private key is NOT part of this repository and must never be committed.
It should be generated once (see the snippet in README/ops docs) and kept
somewhere safe — e.g. a password manager or a secret store outside of git —
since only that key can produce signatures the deployed agents will accept.
"""

import argparse
import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PATH = REPO_ROOT / "src" / "web" / "static" / "client_agent.py"
ROOT_COPY_PATH = REPO_ROOT / "client_agent.py"
SIG_PATH = REPO_ROOT / "src" / "web" / "static" / "client_agent.py.sig"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key", required=True,
        help="Path to the Ed25519 private key PEM used to sign releases.",
    )
    args = parser.parse_args()

    key_bytes = Path(args.key).read_bytes()
    private_key = load_pem_private_key(key_bytes, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        print("✗ Key file is not an Ed25519 private key", file=sys.stderr)
        return 1

    source = AGENT_PATH.read_bytes()
    signature = private_key.sign(source)
    SIG_PATH.write_text(base64.b64encode(signature).decode("ascii") + "\n")

    # Keep the root-level copy (used for local/manual runs) byte-identical
    # to the served static copy, same as every prior client_agent.py change.
    if ROOT_COPY_PATH.exists():
        ROOT_COPY_PATH.write_bytes(source)

    print(f"✓ Signed {AGENT_PATH.relative_to(REPO_ROOT)} -> {SIG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
