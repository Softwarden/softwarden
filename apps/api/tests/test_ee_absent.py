"""Without the optional `ee` package (SAML single sign-on and SCIM provisioning; softwarden/ee.py), which isn't part of
this repository: no organization has `sso.saml` or `scim` whatever its edition, their routes aren't served, the
platform console doesn't offer them, and every place that would use the package refuses with the capability problem
or answers its defaults, never a 500."""

import base64

import pytest

from core import capabilities as caps
from core import sso
from core.models import SsoProvider, SsoSettings, SsoStatus

pytestmark = pytest.mark.skipif(caps.ee_installed(), reason="the optional ee package is installed")


def test_no_edition_has_saml_or_scim(a):
    for edition in ("community", "cloud", "enterprise"):
        org, _ = caps.set_edition(a["org"], edition)
        assert not caps.capabilities(org) & {"sso.saml", "scim"}, edition
    assert not {"sso.saml", "scim"} & {row["name"] for row in caps.catalogue()}


def test_an_override_cant_name_them(a):
    from core.problems import FieldErrors

    for name in ("sso.saml", "scim"):
        with pytest.raises(FieldErrors):
            caps.check_capability_name(name)


def test_the_saml_preset_and_the_scim_settings_are_refused_with_the_capability_problem(a, api):
    caps.set_edition(a["org"], "enterprise")
    admin = api(a["users"]["admin"])
    saml = admin.patch("sso/settings", {"provider": "saml"})
    assert saml.status_code == 403 and saml.json_body["capability"] == "sso.saml"
    values = admin.patch("sso/settings", {"idp_entity_id": "https://idp.example.test"})
    assert values.status_code == 403 and values.json_body["capability"] == "sso.saml"
    scim = admin.patch("organization", {"scim_default_role": "editor"})
    assert scim.status_code == 403 and scim.json_body["capability"] == "scim"


def test_the_organization_and_sign_in_settings_read_without_the_package(a, api):
    caps.set_edition(a["org"], "enterprise")
    admin = api(a["users"]["admin"])
    org = admin.get("organization")
    assert org.status_code == 200
    assert org.json_body["scim_endpoint"] is None and org.json_body["scim_default_role"] == "viewer"
    assert org.json_body["scim_role_groups"] == {"admin": None, "editor": None}
    settings = admin.get("sso/settings")
    assert settings.status_code == 200 and settings.json_body["saml"] is None


def test_saml_settings_saved_earlier_sign_nobody_in_and_still_read(a, api):
    caps.set_edition(a["org"], "enterprise")
    cert = base64.b64encode(b"not a real certificate").decode()
    SsoSettings.objects.create(
        organization=a["org"],
        status=SsoStatus.ACTIVE,
        provider=SsoProvider.SAML,
        issuer="saml:x:https://idp.example.test",
        saml_entity_id="https://idp.example.test",
        saml_sso_url="https://idp.example.test/sso",
        saml_certs=[cert],
        active_revision=1,
    )
    assert sso.effective(a["org"]) is None
    body = api(a["users"]["admin"]).get("sso/settings")
    assert body.status_code == 200 and body.json_body["active"]["idp_certificates"] == []
    assert api().get(f"auth/options?org={a['org'].slug}").status_code == 200


def test_the_saml_and_scim_routes_arent_served(a, api):
    admin = api(a["users"]["admin"])
    for path in ("auth/saml/start", f"auth/saml/{a['org'].slug}/metadata", "scim/tokens", "scim/users"):
        assert admin.get(path).status_code == 404, path
    response = api().client.get(f"/scim/v2/{a['org'].slug}/Users")
    assert not response.get("Content-Type", "").startswith("application/scim+json")
