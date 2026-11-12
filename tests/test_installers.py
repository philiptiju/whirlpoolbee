import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["client-install.sh", "client-install.ps1"])
def test_served_installer_matches_source(name):
    assert (ROOT / name).read_bytes() == (ROOT / "src/web/static" / name).read_bytes()


@pytest.mark.parametrize("suffix", ["sh", "ps1"])
@pytest.mark.parametrize("explicit,environment,expected", [
    (None, None, None),
    ("https://private.example/app/", None, "https://private.example/app"),
    (None, "https://environment.example/app/", "https://environment.example/app"),
    ("https://explicit.example", "https://environment.example", "https://explicit.example"),
    ("file:///wrong", None, None),
])
def test_installer_resolves_server_before_side_effects(tmp_path, suffix, explicit, environment, expected):
    source = (ROOT / f"client-install.{suffix}").read_text()
    # Execute only argument resolution, before any service or filesystem mutation.
    prelude = source.split("# \u2500\u2500 Stop any previously-installed agent", 1)[0]
    assert len(prelude) < len(source)
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.pop("CLAUDE_DASHBOARD_SERVER", None)
    if environment:
        env["CLAUDE_DASHBOARD_SERVER"] = environment
    script = tmp_path / f"prelude.{suffix}"
    if suffix == "sh":
        script.write_text(prelude + '\nprintf "%s" "$SERVER_URL"\n')
        command = ["bash", str(script)]
        if explicit:
            command += ["--server", explicit]
        assert 'DOWNLOAD_SERVER="$SERVER_URL"' in source
    else:
        if not shutil.which("pwsh"):
            pytest.skip("PowerShell is not installed")
        script.write_text(prelude + "\nWrite-Output $ServerUrl\n")
        command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)]
        if explicit:
            command += ["-ServerUrl", explicit]
        assert "$downloadServer = $ServerUrl" in source
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    if expected:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
    else:
        assert result.returncode != 0
        assert "CLAUDE_DASHBOARD_SERVER" in result.stderr
