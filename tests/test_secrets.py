from src.utils.secrets import contains_secrets, get_secret_locations, redact_secrets


def test_secret_helpers_import_and_redact():
    text = "example sk-" + "x" * 24
    assert contains_secrets(text)
    assert get_secret_locations(text)[0]["label"] == "ANTHROPIC_KEY"
    assert redact_secrets(text) == "example [REDACTED_ANTHROPIC_KEY]"
