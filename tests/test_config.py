"""Configuration from the environment: admin sign-in with OIDC (AD-6)."""

from __future__ import annotations

import pytest

from tellybox.config import OidcConfig, oidc_from_env

COMPLETE = {
    "TELLYBOX_OIDC_ISSUER": "https://id.example.org",
    "TELLYBOX_OIDC_CLIENT_ID": "tellybox",
    "TELLYBOX_OIDC_REDIRECT_URI": "https://tellybox.example.org/admin/oidc/callback",
    "TELLYBOX_OIDC_ADMIN_GROUP": "tellybox-admins",
}


def test_off_without_an_issuer():
    assert oidc_from_env({}) is None
    assert oidc_from_env({"TELLYBOX_OIDC_ISSUER": " ", "TELLYBOX_OIDC_CLIENT_ID": "tellybox"}) is None


def test_complete_with_defaults():
    oidc = oidc_from_env(COMPLETE)
    assert oidc == OidcConfig(
        issuer="https://id.example.org",
        client_id="tellybox",
        redirect_uri="https://tellybox.example.org/admin/oidc/callback",
        admin_group="tellybox-admins",
    )
    assert oidc.client_secret is None  # a public client: PKCE only
    assert oidc.groups_claim == "groups"
    assert oidc.scopes == "openid profile email groups"


@pytest.mark.parametrize("name", ["TELLYBOX_OIDC_CLIENT_ID", "TELLYBOX_OIDC_REDIRECT_URI", "TELLYBOX_OIDC_ADMIN_GROUP"])
def test_each_required_variable_is_named_when_missing(name):
    env = {**COMPLETE, name: ""}
    with pytest.raises(ValueError, match=name):
        oidc_from_env(env)
    del env[name]
    with pytest.raises(ValueError, match=name):
        oidc_from_env(env)


def test_all_missing_variables_are_named_at_once():
    with pytest.raises(ValueError) as exc:
        oidc_from_env({"TELLYBOX_OIDC_ISSUER": "https://id.example.org"})
    assert all(name in str(exc.value) for name in
               ("TELLYBOX_OIDC_CLIENT_ID", "TELLYBOX_OIDC_REDIRECT_URI", "TELLYBOX_OIDC_ADMIN_GROUP"))


@pytest.mark.parametrize("uri", [
    "https://tellybox.example.org/admin/login",
    "/admin/oidc/callback",
    "tellybox.example.org/admin/oidc/callback",
])
def test_redirect_uri_must_be_a_full_url_to_the_callback(uri):
    with pytest.raises(ValueError, match="TELLYBOX_OIDC_REDIRECT_URI"):
        oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_REDIRECT_URI": uri})


def test_issuer_must_be_a_url():
    with pytest.raises(ValueError, match="TELLYBOX_OIDC_ISSUER"):
        oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_ISSUER": "id.example.org"})


def test_client_secret_directly():
    oidc = oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_CLIENT_SECRET": "s3cret"})
    assert oidc.client_secret == "s3cret"
    assert "s3cret" not in repr(oidc)


def test_client_secret_from_a_file(tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text("from-file\n")
    oidc = oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_CLIENT_SECRET_FILE": str(path)})
    assert oidc.client_secret == "from-file"


def test_scopes_and_groups_claim():
    oidc = oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_SCOPES": "  openid   profile roles ",
                          "TELLYBOX_OIDC_GROUPS_CLAIM": "roles"})
    assert oidc.scopes == "openid profile roles"
    assert oidc.groups_claim == "roles"


def test_scopes_without_openid_are_refused():
    with pytest.raises(ValueError, match="openid"):
        oidc_from_env({**COMPLETE, "TELLYBOX_OIDC_SCOPES": "profile groups"})
