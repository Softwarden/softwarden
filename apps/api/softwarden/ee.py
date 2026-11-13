"""Whether the optional `ee` package (SAML single sign-on, SCIM provisioning) is installed beside the API.

It isn't part of this repository. When an installation adds it, settings install its Django apps, the URLconfs
include its routes and core/capabilities.py offers `sso.saml` and `scim` to the editions that have them. Without
it, no organization has either capability, and every place that would use the package refuses with the usual
capability problem first. No Django import here: settings read it."""

import importlib.util

INSTALLED = importlib.util.find_spec("ee") is not None
