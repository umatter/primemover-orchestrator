"""Secrets never live in a lock and never leak out of a resolve."""

from pathlib import Path

import pytest

from experiment_designer.lock.secrets import (
    InsecureSecretsFile,
    MissingSecret,
    SecretStore,
    find_literal_credentials,
    find_placeholders,
    resolve_secrets,
)


@pytest.fixture
def store_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.toml"
    p.write_text('[oxylabs.us_nc]\nusername = "cust-nc"\npassword = "testpass"\n')
    p.chmod(0o600)
    return p


def test_find_placeholders_walks_nested_structures():
    obj = {"a": [{"b": "${secret:x.y}"}, "plain"], "c": {"d": "${secret:z}"}}
    assert find_placeholders(obj) == {"x.y", "z"}


def test_literal_credentials_are_reported_by_path():
    crawlers = [
        {"proxy": {"type": "http", "username": "${secret:a.u}", "password": "hunter2"}},
        {"proxy": {"type": "oxylabs", "username": "", "password": ""}},
        {"proxy": {"type": "http", "username": "", "password": ""}},
    ]
    assert find_literal_credentials(crawlers) == ["crawlers[0].proxy.password"]


def test_credential_with_an_embedded_placeholder_is_not_a_literal():
    """Oxylabs puts geo/session targeting *around* the account in the username:
    customer-<user>-cc-us-city-charlotte-sessid-b1. The account is the secret;
    the rest is not, so a value that contains a placeholder is a templated
    credential, not a literal one."""
    crawlers = [
        {
            "proxy": {
                "type": "http",
                "username": "customer-${secret:oxylabs.user}-cc-us-city-charlotte-sessid-b1",
                "password": "${secret:oxylabs.password}",
            }
        }
    ]
    assert find_literal_credentials(crawlers) == []


def test_self_configuring_proxy_may_carry_literals():
    crawlers = [{"proxy": {"type": "oxylabs", "username": "acct", "password": "testpass"}}]
    assert find_literal_credentials(crawlers) == []


def test_store_resolves_dotted_keys(store_file: Path):
    store = SecretStore.load(store_file)
    assert store.get("oxylabs.us_nc.username") == "cust-nc"
    assert store.missing(["oxylabs.us_nc.password", "nope.x"]) == ["nope.x"]


def test_missing_key_names_the_key(store_file: Path):
    with pytest.raises(MissingSecret, match="nope.x"):
        SecretStore.load(store_file).get("nope.x")


def test_group_readable_file_is_refused(store_file: Path):
    store_file.chmod(0o640)
    with pytest.raises(InsecureSecretsFile, match="chmod 600"):
        SecretStore.load(store_file)


def test_default_path_comes_from_env(store_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(store_file))
    assert SecretStore.load().get("oxylabs.us_nc.password") == "testpass"


def test_resolve_replaces_only_placeholders_and_does_not_mutate(store_file: Path):
    store = SecretStore.load(store_file)
    obj = {"proxy": {"username": "${secret:oxylabs.us_nc.username}", "host": "h"}}
    out = resolve_secrets(obj, store)
    assert out == {"proxy": {"username": "cust-nc", "host": "h"}}
    assert obj["proxy"]["username"].startswith("${secret:")


def test_non_string_leaf_in_store_is_an_error(tmp_path: Path):
    p = tmp_path / "s.toml"
    p.write_text("[a]\nb = 5\n")
    p.chmod(0o600)
    with pytest.raises(MissingSecret, match="a.b"):
        SecretStore.load(p).get("a.b")
