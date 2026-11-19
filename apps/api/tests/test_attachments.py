"""Attachments (catalog/attachments.py, catalog/attachment_viewsets.py): uploads and
links, content checks and limits, roles and download-by-guess, download headers, audit with the parent as related
target, the sweep and verify commands, the upload rate limit, tenancy and query counts. Every test has its own empty
MEDIA_ROOT (tests/conftest.py media_root). FACTORIES in test_drf_resources.py runs the scoping, read-only token and
If-Match sweeps over the new routes."""

import hashlib
import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog import attachments, customfields, services
from catalog.account_serializers import AttachmentLimitsSerializer
from catalog.models import Attachment, Resource, RetiredFile, Vendor
from core import jobs
from core.models import AuditEvent
from tests.test_ai_agents import complete, model, provider

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
STORED = re.compile(r"^attachments/[0-9a-f-]{36}/[0-9a-f]{32}$")


def docx_bytes(part="word/document.xml"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, "<document/>")
    return buffer.getvalue()


def snap(value: Any) -> dict[str, Any]:
    """A snapshot that must be there."""
    assert value is not None
    return value


def upload(client, path, name, data, **fields):
    """A multipart upload through the API (the JSON test helper can't send files)."""
    response = client.client.post(f"/api/v1/{path}", {"file": SimpleUploadedFile(name, data), **fields})
    response.json_body = response.json() if response.content else None
    return response


def stored_path(media_root, attachment) -> Path:
    return Path(media_root) / attachment.storage_name


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()  # the upload throttle counts in the process cache
    yield
    cache.clear()


# ---- uploads and links -----------------------------------------------------------------------------------------------


def test_an_editor_uploads_a_pdf_and_every_role_downloads_it(a, api, media_root):
    editor = api(a["users"]["editor"])
    created = upload(editor, f"applications/{a['app'].id}/attachments", "Contract 2026.pdf", PDF, category="contract")
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["kind"] == "file" and body["title"] == "Contract 2026.pdf" and body["category"] == "contract"
    assert body["filename"] == "Contract 2026.pdf" and body["content_type"] == "application/pdf"
    assert body["size"] == len(PDF) and body["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert body["url"] == "" and body["download_url"] == f"/api/v1/attachments/{body['id']}/download"
    assert body["attached_to"] == {"type": "application", "id": str(a["app"].id), "name": a["app"].name}
    assert body["uploaded_by"]["id"] == a["users"]["editor"].id
    row = Attachment.objects.get(id=body["id"])
    assert row.organization_id == a["org"].id and row.resource_id == a["app"].id
    # Stored under an unpredictable name with nothing of the original, and readable only through the download route.
    assert STORED.match(row.storage_name) and row.storage_name.startswith(f"attachments/{a['org'].id}/")
    assert "Contract" not in row.storage_name and "storage_name" not in body
    assert stored_path(media_root, row).read_bytes() == PDF
    for role in ("viewer", "editor", "admin"):
        client = api(a["users"][role])
        assert client.get(f"applications/{a['app'].id}/attachments").json_body[0]["id"] == body["id"]
        response = client.client.get(body["download_url"])
        assert response.status_code == 200, role
        assert b"".join(response.streaming_content) == PDF


def test_download_headers_never_show_a_file_inline(a, api):
    editor = api(a["users"]["editor"])
    made = upload(editor, f"applications/{a['app'].id}/attachments", 'Résumé "final".pdf', PDF).json_body
    response = api(a["users"]["viewer"]).client.get(made["download_url"], HTTP_ACCEPT="text/html")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"  # its own, not the SPA's
    assert response["X-Frame-Options"] == "DENY"
    assert response["Content-Length"] == str(len(PDF))
    disposition = response["Content-Disposition"]
    assert disposition.startswith("attachment; ") and "inline" not in disposition
    assert 'filename="Resume _final_.pdf"' in disposition  # an ASCII fallback with no quote to break out of
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20%22final%22.pdf" in disposition  # the exact name (RFC 5987)


def test_markup_saved_as_text_downloads_as_inert_text(a, api):
    """UTF-8 text may hold HTML (a .txt of a web page passes the text check), so the download is what makes it inert:
    a text type the check chose, nosniff, the sandboxing CSP (Caddy keeps the app's own) and attachment disposition."""
    page = b"<!doctype html><script>alert(document.cookie)</script>"
    made = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "page.txt", page).json_body
    assert made["content_type"] == "text/plain; charset=utf-8"
    response = api(a["users"]["viewer"]).client.get(made["download_url"], HTTP_ACCEPT="text/html")
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response["Content-Disposition"].startswith("attachment; ")


def test_content_disposition_keeps_names_safe():
    assert attachments.content_disposition("report.pdf") == (
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    )
    header = attachments.content_disposition("a\r\nSet-Cookie: x=1;.txt")
    assert "\r" not in header and "\n" not in header
    assert attachments.clean_filename("../../etc/passwd.txt") == "passwd.txt"
    assert attachments.clean_filename("C:\\Users\\me\\notes\u202e.txt") == "notes.txt"
    assert attachments.clean_filename("x" * 300 + ".pdf").endswith("x.pdf")
    assert len(attachments.clean_filename("x" * 300 + ".pdf")) == attachments.FILENAME_MAX


def test_the_clients_content_type_is_ignored(a, api):
    editor = api(a["users"]["editor"])
    response = editor.client.post(
        f"/api/v1/applications/{a['app'].id}/attachments",
        {"file": SimpleUploadedFile("diagram.png", PNG, content_type="text/html")},
    )
    assert response.status_code == 201 and response.json()["content_type"] == "image/png"


def test_a_sharepoint_link_is_added_with_json(a, api):
    editor = api(a["users"]["editor"])
    url = "https://contoso.sharepoint.com/sites/IT/Shared%20Documents/Runbook.docx"
    created = editor.post(
        f"applications/{a['app'].id}/attachments", {"url": url, "title": "Runbook", "category": "sop_runbook"}
    )
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["kind"] == "link" and body["url"] == url and body["download_url"] is None
    assert body["size"] is None and body["sha256"] == "" and body["filename"] == ""
    untitled = editor.post(f"applications/{a['app'].id}/attachments", {"url": "https://example.test/page"}).json_body
    assert untitled["title"] == "https://example.test/page" and untitled["category"] == "other"
    # A link has no file to download.
    assert api(a["users"]["viewer"]).client.get(f"/api/v1/attachments/{body['id']}/download").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://files.example.test/x",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "",
        None,
        "https://svc-account:Winter2026!@contoso.sharepoint.com/sites/IT",  # a credential in the link
        "https://svc-account@contoso.sharepoint.com/sites/IT",
    ],
)
def test_links_are_http_or_https_only(a, api, url):
    response = api(a["users"]["editor"]).post(f"applications/{a['app'].id}/attachments", {"url": url})
    assert response.status_code == 400 and "url" in response.json_body["errors"]
    assert not Attachment.objects.exists()


def test_titles_and_categories_are_validated(a, api):
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    bad = editor.post(path, {"url": "https://example.test", "title": "x" * 201, "category": "secret_plans"})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"title", "category"}
    wrong_kind = editor.post(path, {"kind": "file", "url": "https://example.test"})
    assert wrong_kind.status_code == 400 and "kind" in wrong_kind.json_body["errors"]
    missing = editor.client.post(f"/api/v1/{path}", {"title": "No file"})
    assert missing.status_code == 400 and "file" in missing.json()["errors"]


def test_an_edit_changes_title_category_and_a_links_address_only(a, api):
    editor = api(a["users"]["editor"])
    link = editor.post(f"applications/{a['app'].id}/attachments", {"url": "https://example.test/a"}).json_body
    changed = editor.patch(
        f"attachments/{link['id']}", {"title": "Architecture", "category": "architecture", "url": "https://x.test/b"}
    )
    assert changed.status_code == 200, changed.json_body
    assert changed.json_body["title"] == "Architecture" and changed.json_body["url"] == "https://x.test/b"
    assert changed.json_body["category"] == "architecture"
    file = upload(editor, f"applications/{a['app'].id}/attachments", "notes.txt", b"hello").json_body
    refused = editor.patch(f"attachments/{file['id']}", {"url": "https://x.test", "sha256": "0" * 64, "title": ""})
    assert refused.status_code == 400 and set(refused.json_body["errors"]) == {"url", "sha256", "title"}
    before = AuditEvent.objects.count()
    assert editor.patch(f"attachments/{file['id']}", {"title": "notes.txt"}).status_code == 200
    assert AuditEvent.objects.count() == before  # nothing changed, nothing written


# ---- content checks and limits ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, data, accepted",
    [
        ("contract.pdf", PDF, True),
        ("contract.pdf", PNG, False),  # not a PDF
        ("diagram.png", PNG, True),
        ("diagram.png", b"just text", False),
        ("photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 16, True),
        ("photo.jpg", PDF, False),
        ("design.docx", docx_bytes(), True),
        ("design.docx", docx_bytes("xl/workbook.xml"), False),  # an xlsx renamed
        ("design.docx", b"PK\x03\x04 not really a zip", False),
        ("sheet.xlsx", docx_bytes("xl/workbook.xml"), True),
        ("bundle.zip", docx_bytes(), True),
        ("legacy.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16, True),
        ("legacy.doc", b"MZ\x90\x00 a program", False),
        ("notes.txt", "Café notes".encode(), True),
        ("notes.txt", b"MZ\x90\x00\x03\x00", False),  # binary
        ("notes.txt", "caf\xe9".encode("latin-1"), False),  # not UTF-8
        ("data.csv", b"name,owner\nPortal,Dana\n", True),
        ("readme.md", b"# Runbook\n", True),
        ("empty.txt", b"", False),
        # Markup that a browser could render, behind an allowed extension (upload sniffing).
        ("diagram.png", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', False),
        ("photo.gif", b"<!doctype html><script>alert(1)</script>", False),
        ("contract.pdf", b"<!doctype html><html><body>Invoice</body></html>", False),
    ],
)
def test_content_must_match_the_extension(a, api, name, data, accepted):
    response = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", name, data)
    assert (response.status_code == 201) is accepted, response.json_body
    if not accepted:
        assert set(response.json_body["errors"]) == {"file"}
        assert not Attachment.objects.exists()


@pytest.mark.parametrize("name", ["page.html", "logo.svg", "setup.exe", "script.js", "run.sh", "noextension", ".pdf"])
def test_types_that_could_run_or_render_are_never_accepted(a, api, name):
    response = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", name, b"<html></html>")
    assert response.status_code == 400 and "file" in response.json_body["errors"]


def test_only_extensions_with_a_content_check_can_be_configured(settings):
    settings.ATTACHMENT_EXTENSIONS = ["pdf", "html", "svg"]
    errors = attachments.check_settings()
    assert [e.id for e in errors] == ["softwarden.E101"] and "html, svg" in errors[0].msg
    settings.ATTACHMENT_EXTENSIONS = ["pdf"]
    assert attachments.check_settings() == []


def test_the_size_limit_is_checked_while_reading(a, api, settings):
    settings.ATTACHMENT_MAX_SIZE_MB = 1
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    exactly = upload(editor, path, "big.txt", b"a" * attachments.MB)
    assert exactly.status_code == 201, exactly.json_body
    over = upload(editor, path, "bigger.txt", b"a" * (attachments.MB + 1))
    assert over.status_code == 400 and over.json_body["errors"]["file"] == ["The file is larger than 1 MB."]

    class Lying(SimpleUploadedFile):  # a size the client misreported: the count while reading still refuses it
        size = 10

    with pytest.raises(Exception) as refused:
        attachments.upload_file(
            a["org"], a["users"]["editor"], a["app"], Lying("x.txt", b"a" * (2 * attachments.MB)), {}
        )
    assert getattr(refused.value, "errors", None) == {"file": ["The file is larger than 1 MB."]}
    assert Attachment.objects.count() == 1


@pytest.fixture
def parse_spy(monkeypatch):
    """Every multipart parse, Django's (request.POST, which CSRF reads) and DRF's (request.data), recorded."""
    from django.http import multipartparser
    from rest_framework import parsers

    calls: list[str] = []
    django_parse, drf_parse = multipartparser.MultiPartParser.parse, parsers.MultiPartParser.parse

    def django_spy(self, *args, **kwargs):
        calls.append("django")
        return django_parse(self, *args, **kwargs)

    def drf_spy(self, *args, **kwargs):
        calls.append("drf")
        return drf_parse(self, *args, **kwargs)

    monkeypatch.setattr(multipartparser.MultiPartParser, "parse", django_spy)
    monkeypatch.setattr(parsers.MultiPartParser, "parse", drf_spy)
    return calls


def csrf_client(user):
    """A browser-like session client: CSRF enforced, the token from GET auth/csrf sent back in the header."""
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/api/v1/auth/csrf")
    return client, client.cookies["csrftoken"].value


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_an_oversize_body_is_refused_before_anything_parses_it(a, settings, parse_spy, role):
    """CSRF reads request.POST for a session client before authentication, the role check or the view; the request
    limit (core/upload_limit.py) answers first, so nothing reads the body, whoever sends it."""
    settings.MAX_REQUEST_MB = 1
    client, token = csrf_client(a["users"][role])
    big = SimpleUploadedFile("big.txt", b"a" * (2 * attachments.MB))
    response = client.post(f"/api/v1/applications/{a['app'].id}/attachments", {"file": big}, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 413 and response["Content-Type"] == "application/problem+json"
    assert response.json()["detail"] == "The upload is larger than this installation accepts (1 MB)."
    assert parse_spy == []
    assert not Attachment.objects.exists()
    # The same client and token get through with a small file (the editor) or to the role check (the viewer).
    small = client.post(
        f"/api/v1/applications/{a['app'].id}/attachments",
        {"file": SimpleUploadedFile("n.txt", b"x")},
        HTTP_X_CSRFTOKEN=token,
    )
    assert small.status_code == (201 if role == "editor" else 403)


def test_a_multipart_body_needs_a_content_length():
    from django.http import HttpResponse
    from django.test import RequestFactory

    from core.upload_limit import RequestSizeLimitMiddleware

    middleware = RequestSizeLimitMiddleware(lambda request: HttpResponse("reached"))
    request = RequestFactory().post("/api/v1/applications/x/attachments", {"file": SimpleUploadedFile("n.txt", b"x")})
    for missing in ("", "abc", "-1"):
        request.META["CONTENT_LENGTH"] = missing
        assert middleware(request).status_code == 411
    del request.META["CONTENT_LENGTH"]
    assert middleware(request).status_code == 411
    json_post = RequestFactory().post("/api/v1/applications", {}, content_type="application/json")
    assert middleware(json_post).content == b"reached"  # only multipart is limited here


@pytest.mark.parametrize("path", ["/", "/applications/new", "/healthz", "/api/v2/anything"])
def test_every_multipart_request_is_limited_whatever_its_path(settings, parse_spy, path):
    """The SPA catch-all and every other route would parse a multipart body too (CSRF reads request.POST), signed in or
    not, so the limit doesn't depend on the path or the method."""
    from django.test import Client

    settings.MAX_REQUEST_MB = 1
    client = Client(enforce_csrf_checks=True)  # signed out
    big = SimpleUploadedFile("big.txt", b"a" * (2 * attachments.MB))
    for method in ("post", "put", "patch"):
        if method == "post":
            response = client.post(path, {"file": big})
        else:  # the test client sends these with the content type given; build a multipart body by hand
            from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

            big.seek(0)
            body = encode_multipart(BOUNDARY, {"file": big})
            response = getattr(client, method)(path, body, content_type=MULTIPART_CONTENT)
        assert response.status_code == 413, (method, path)
        assert response["Content-Type"] == "application/problem+json"
    assert parse_spy == []


def test_the_view_refuses_a_file_over_the_organizations_own_limit_before_scanning_it(a, api, settings, monkeypatch):
    """An organization limit below the installation's: the request limit lets the body through (it is parsed then),
    and the view's Content-Length check refuses it before the content check or storage (scan is never called)."""
    settings.MAX_REQUEST_MB = 26
    settings.ATTACHMENT_MAX_SIZE_MB = 25
    a["org"].settings = {"attachment_max_size_mb": 1}
    a["org"].save()
    read = []
    monkeypatch.setattr(attachments, "scan", lambda *args: read.append(args))
    response = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "x.txt", b"a" * (2 << 20))
    assert response.status_code == 400 and response.json_body["errors"]["file"] == ["The file is larger than 1 MB."]
    assert read == []


def test_the_request_limit_must_fit_the_attachment_limit(settings):
    settings.ATTACHMENT_MAX_SIZE_MB = 25
    settings.MAX_REQUEST_MB = 25
    assert [e.id for e in attachments.check_settings()] == ["softwarden.E105"]
    settings.MAX_REQUEST_MB = 26
    assert attachments.check_settings() == []
    settings.ATTACHMENT_MAX_SIZE_MB = 1
    settings.MAX_REQUEST_MB = 2  # too small for a 5 MB CSV import
    assert [e.id for e in attachments.check_settings()] == ["softwarden.E105"]


def test_backups_archive_and_restore_the_media_volume_encrypted():
    """ops/backup.sh streams a tar of the volume straight into the same openssl step as the dump (nothing readable on
    disk), tolerating only tar's "file changed" exit, after the dump's checksum is written. ops/restore.sh checks the
    media archive (listed in the checksums, decrypts, lists) before stopping anything, stages it inside the volume and
    swaps only after the staging succeeded, then runs verify_attachments. The shell steps themselves were exercised in
    a throwaway container during review (a staged swap keeps owner and mode; a failed stage leaves the files)."""
    import shutil
    import subprocess

    ops = Path(__file__).resolve().parents[3] / "ops"
    backup, restore = (ops / "backup.sh").read_text(encoding="utf-8"), (ops / "restore.sh").read_text(encoding="utf-8")
    media_step = backup.index('step="archiving attachment files"')
    assert backup.index('step="writing checksums"') < media_step  # the dump's checksum never waits for the media
    # media/exports (organization exports and tenant backups) is left out, as the worker's backup does.
    assert (
        "tar --warning=no-file-changed --exclude=media/.incoming --exclude=media/exports -C /data -cf - media" in backup
    )
    assert '[ "$rc" -le 1 ] || exit "$rc"' in backup
    assert '| aes_encrypt_stdin "$part_file"' in backup[media_step:]
    assert '"${stem:?}.media.tar.enc"' in backup and 'files+=("$name.media.tar.enc")' in backup
    # Restore: every check before app and worker stop; staging before anything is deleted.
    stop = restore.index('step="stopping app and worker"')
    assert restore.index('step="checking the media archive\'s checksum"') < stop
    assert restore.index('step="checking the media archive decrypts"') < stop
    stage, swap = restore.index('step="staging attachment files"'), restore.index('step="replacing attachment files"')
    assert stop < stage < swap
    assert "-delete" not in restore and "rm -rf {} +" not in restore[:swap]
    assert "tar -C /data/media/.incoming -xpf -" in restore[stage:swap]
    # The live restore verifies after the swap; the rehearsal (earlier in the script) verifies the archive
    # unpacked in its own container, never the volume: in a throwaway volume of that container, since its root
    # filesystem is read-only and /tmp a small tmpfs.
    assert restore.rindex("manage.py verify_attachments") > swap
    rehearsal = restore[: restore.index('step="staging attachment files"')]
    assert "rehearse_run -v /data/rehearsal -e SOFTWARDEN_MEDIA_ROOT=/data/rehearsal/media" in rehearsal
    shell = shutil.which("bash")
    if shell:
        for script in ("backup.sh", "restore.sh"):
            assert subprocess.run([shell, "-n", str(ops / script)], check=False).returncode == 0, script  # noqa: S603


def test_organization_limits_can_only_tighten_the_installations(a, api, settings):
    settings.ATTACHMENT_MAX_SIZE_MB = 25
    settings.ATTACHMENT_QUOTA_MB = 100
    settings.ATTACHMENT_RETENTION_DAYS = 9
    admin = api(a["users"]["admin"])
    shown = admin.get("organization").json_body["attachments"]
    assert shown["max_size_mb"] == 25 and shown["quota_mb"] == 100 and shown["used_bytes"] == 0
    assert shown["retention_days"] == 9  # the Remove dialog names it
    assert set(shown) == set(AttachmentLimitsSerializer().fields)
    assert "pdf" in shown["allowed_extensions"] and shown["installation"]["max_size_mb"] == 25
    refused = admin.patch(
        "organization",
        {"attachment_max_size_mb": 26, "attachment_extensions": ["pdf", "html"], "attachment_quota_mb": 101},
    )
    assert refused.status_code == 400
    assert set(refused.json_body["errors"]) == {
        "attachment_max_size_mb",
        "attachment_extensions",
        "attachment_quota_mb",
    }
    ok = admin.patch(
        "organization",
        {"attachment_max_size_mb": 1, "attachment_extensions": ["PDF", ".txt"], "attachment_quota_mb": 2},
    )
    assert ok.status_code == 200, ok.json_body
    limits = ok.json_body["attachments"]
    assert limits["max_size_mb"] == 1 and limits["allowed_extensions"] == ["pdf", "txt"] and limits["quota_mb"] == 2
    event = AuditEvent.objects.filter(action="organization.updated").latest("created_at")
    assert event.changes["attachment_max_size_mb"] == [None, 1]
    assert snap(event.postchange)["settings"]["attachment_extensions"] == ["pdf", "txt"]
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    assert upload(editor, path, "d.png", PNG).status_code == 400  # png no longer allowed here
    assert upload(editor, path, "n.txt", b"a" * (attachments.MB + 1)).status_code == 400
    assert api(a["users"]["editor"]).patch("organization", {"attachment_max_size_mb": 5}).status_code == 403
    reset = admin.patch("organization", {"attachment_max_size_mb": None, "attachment_extensions": None})
    assert (
        reset.json_body["attachments"]["max_size_mb"] == 25
        and "png" in reset.json_body["attachments"]["allowed_extensions"]
    )
    # An installation that later lowers its own limit lowers the organization's too.
    settings.ATTACHMENT_QUOTA_MB = 1
    assert attachments.limits_for(type(a["org"]).objects.get(pk=a["org"].pk)).quota_mb == 1


def test_the_quota_counts_every_file_of_the_organization(a, b, api, settings):
    settings.ATTACHMENT_QUOTA_MB = 1
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    assert upload(editor, path, "a.txt", b"a" * 700_000).status_code == 201
    over = upload(editor, path, "b.txt", b"b" * 400_000)
    assert over.status_code == 400 and "quota" in over.json_body["errors"]["file"][0]
    assert editor.post(path, {"url": "https://example.test"}).status_code == 201  # links take no space
    other = upload(api(b["users"]["editor"]), f"applications/{b['app'].id}/attachments", "b.txt", b"b" * 400_000)
    assert other.status_code == 201  # each organization has its own quota


# ---- roles and download-by-guess ---------------------------------------------------------------------------------------


def test_a_viewer_reads_and_downloads_but_never_writes(a, api):
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    path = f"applications/{a['app'].id}/attachments"
    made = upload(editor, path, "notes.txt", b"hello").json_body
    assert upload(viewer, path, "mine.txt", b"x").status_code == 403
    assert viewer.post(path, {"url": "https://example.test"}).status_code == 403
    assert viewer.patch(f"attachments/{made['id']}", {"title": "Mine"}).status_code == 403
    assert viewer.delete(f"attachments/{made['id']}").status_code == 403
    assert viewer.get(f"attachments/{made['id']}").status_code == 200
    assert viewer.client.get(made["download_url"]).status_code == 200
    assert Attachment.objects.count() == 1
    assert editor.delete(f"attachments/{made['id']}").status_code == 204


def test_another_organizations_attachment_is_the_same_404_as_none(a, b, api):
    theirs = upload(api(b["users"]["editor"]), f"applications/{b['app'].id}/attachments", "b.txt", b"theirs").json_body
    viewer = api(a["users"]["viewer"])
    for path in (f"attachments/{theirs['id']}", f"attachments/{uuid.uuid4()}"):
        for suffix in ("", "/download"):
            response = viewer.client.get(f"/api/v1/{path}{suffix}")
            assert response.status_code == 404 and response["Content-Type"] == "application/problem+json"
            assert response.json()["detail"] == "This record doesn't exist or you don't have access."
    admin = api(a["users"]["admin"])
    assert admin.patch(f"attachments/{theirs['id']}", {"title": "x"}).status_code == 404
    assert admin.delete(f"attachments/{theirs['id']}").status_code == 404
    assert admin.get(f"applications/{b['app'].id}/attachments").status_code == 404
    assert upload(admin, f"applications/{b['app'].id}/attachments", "x.txt", b"x").status_code == 404
    assert Attachment.objects.get(id=theirs["id"]).title == "b.txt"


def test_signed_out_requests_get_nothing(a, api):
    made = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "n.txt", b"x").json_body
    anonymous = api()
    for path in (made["download_url"], f"/api/v1/attachments/{made['id']}"):
        assert anonymous.client.get(path).status_code in (401, 403)


def test_kinds_do_not_cross_routes(a, api):
    editor = api(a["users"]["editor"])
    assert editor.get(f"components/{a['app'].id}/attachments").status_code == 404
    assert editor.get(f"vendors/{a['app'].id}/attachments").status_code == 404


def test_a_missing_stored_file_is_a_404_not_an_error(a, api, media_root):
    made = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "n.txt", b"x").json_body
    stored_path(media_root, Attachment.objects.get(id=made["id"])).unlink()
    response = api(a["users"]["viewer"]).client.get(made["download_url"])
    assert response.status_code == 404 and "verify_attachments" in response.json()["detail"]


# ---- the upload rate limit ---------------------------------------------------------------------------------------------


def test_file_uploads_are_rate_limited_per_user(a, api, settings):
    settings.ATTACHMENT_UPLOAD_RATE = "2/min"
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    path = f"applications/{a['app'].id}/attachments"
    assert upload(editor, path, "1.txt", b"1").status_code == 201
    assert upload(editor, path, "2.txt", b"2").status_code == 201
    third = upload(editor, path, "3.txt", b"3")
    assert third.status_code == 429 and int(third["Retry-After"]) > 0
    assert third["Content-Type"] == "application/problem+json"
    assert editor.post(path, {"url": "https://example.test"}).status_code == 201  # links aren't counted
    assert upload(admin, path, "4.txt", b"4").status_code == 201  # per user
    assert Attachment.objects.filter(kind="file").count() == 3


# ---- audit -------------------------------------------------------------------------------------------------------------


def test_every_write_shows_in_the_parents_history_with_a_diff(a, api):
    editor = api(a["users"]["editor"])
    app = a["app"]
    made = upload(editor, f"applications/{app.id}/attachments", "Runbook.pdf", PDF, category="sop_runbook").json_body
    editor.patch(f"attachments/{made['id']}", {"title": "Ops runbook", "category": "architecture"})
    editor.delete(f"attachments/{made['id']}")
    events = list(AuditEvent.objects.filter(action__startswith="attachment.").order_by("created_at"))
    assert [e.action for e in events] == ["attachment.created", "attachment.updated", "attachment.deleted"]
    for e in events:
        assert (e.target_type, e.target_id) == ("Attachment", uuid.UUID(made["id"]))
        assert (e.related_target_type, e.related_target_id) == ("Resource", app.id)
    created, updated, deleted = events
    post = snap(created.postchange)
    assert created.prechange is None and post["filename"] == "Runbook.pdf"
    assert post["sha256"] == hashlib.sha256(PDF).hexdigest() and post["size"] == len(PDF)
    assert post["resource"] == {"id": str(app.id), "display": app.name}
    assert post["uploaded_by"] == {"id": a["users"]["editor"].id, "display": None}
    assert snap(updated.prechange)["title"] == "Runbook.pdf" and snap(updated.postchange)["title"] == "Ops runbook"
    assert snap(deleted.prechange)["title"] == "Ops runbook" and deleted.postchange is None
    history = editor.get(f"applications/{app.id}/audit").json_body["results"]
    rows = {h["action"]: h for h in history if h["action"].startswith("attachment.")}
    assert rows["attachment.created"]["summary"] == f"Runbook.pdf; Runbook.pdf, {len(PDF)} bytes"
    assert rows["attachment.updated"]["summary"] == "Runbook.pdf → Ops runbook; Category: SOP or runbook → Architecture"
    assert rows["attachment.deleted"]["label"] == "Attachment removed"
    assert rows["attachment.created"]["target"] == {"kind": "application", "id": str(app.id), "name": app.name}
    diff = {r["field"]: r for r in editor.get(f"applications/{app.id}/audit/{updated.id}").json_body["diff"]}
    assert diff["title"]["before"] == "Runbook.pdf" and diff["title"]["after"] == "Ops runbook"
    assert diff["category"]["after"] == "architecture"
    shown = {r["field"]: r for r in editor.get(f"applications/{app.id}/audit/{created.id}").json_body["diff"]}
    assert shown["uploaded_by"]["after"]["display"] == a["users"]["editor"].email  # named when read
    log = api(a["users"]["admin"]).get("audit-events?target_type=Attachment").json_body["results"]
    assert {e["action"] for e in log} == {"attachment.created", "attachment.updated", "attachment.deleted"}


def test_snapshots_never_hold_the_file_or_where_it_is_stored(a, api):
    secret_text = b"Contents that must stay out of the audit log."
    made = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "n.txt", secret_text)
    row = Attachment.objects.get(id=made.json_body["id"])
    event = AuditEvent.objects.get(action="attachment.created")
    dumped = json.dumps([event.changes, event.prechange, event.postchange])
    assert secret_text.decode() not in dumped and row.storage_name not in dumped and "storage_name" not in dumped
    assert set(snap(event.postchange)) == {
        "resource",
        "vendor",
        "contract",
        "kind",
        "title",
        "category",
        "url",
        "filename",
        "content_type",
        "size",
        "sha256",
        "uploaded_by",
    }


def test_vendor_attachments_name_the_vendor(a, api):
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    editor = api(a["users"]["editor"])
    made = editor.post(f"vendors/{vendor.id}/attachments", {"url": "https://contoso.test/msa", "category": "contract"})
    assert made.status_code == 201 and made.json_body["attached_to"]["type"] == "vendor"
    event = AuditEvent.objects.get(action="attachment.created")
    assert (event.related_target_type, event.related_target_id) == ("Vendor", vendor.id)
    log = api(a["users"]["admin"]).get(f"audit-events?related_target={vendor.id}").json_body["results"]
    assert log[0]["target"] == {"kind": "vendor", "id": str(vendor.id), "name": "Contoso"}
    assert log[0]["summary"] == "https://contoso.test/msa; link"


def test_deleting_the_parent_removes_its_attachments_and_retires_their_files(a, api, media_root):
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active"}
    )
    made = upload(editor, f"components/{job.id}/attachments", "n.txt", b"x").json_body
    name = Attachment.objects.get(id=made["id"]).storage_name
    assert admin.delete(f"components/{job.id}").status_code == 204
    assert not Resource.objects.filter(id=job.id).exists() and not Attachment.objects.exists()
    assert RetiredFile.objects.get(name=name).organization_id == a["org"].id
    assert (Path(media_root) / name).exists()  # kept for the retention window
    assert AuditEvent.objects.filter(action="attachment.deleted", related_target_id=job.id).count() == 1
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    editor.post(f"vendors/{vendor.id}/attachments", {"url": "https://contoso.test"})
    assert admin.delete(f"vendors/{vendor.id}").status_code == 204
    assert not Vendor.objects.filter(id=vendor.id).exists() and not Attachment.objects.exists()


# ---- sweep and verify --------------------------------------------------------------------------------------------------


def _backdate(path: Path, days: int) -> None:
    then = time.time() - days * 86400
    os.utime(path, (then, then))


def test_the_sweep_removes_retired_and_orphaned_files_after_the_window(a, api, media_root, settings):
    settings.ATTACHMENT_RETENTION_DAYS = 7
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    kept = Attachment.objects.get(id=upload(editor, path, "kept.txt", b"kept").json_body["id"])
    gone = Attachment.objects.get(id=upload(editor, path, "gone.txt", b"gone").json_body["id"])
    editor.delete(f"attachments/{gone.id}")
    orphan = Path(media_root) / "attachments" / str(a["org"].id) / ("f" * 32)
    orphan.write_bytes(b"left by a rolled-back upload")
    fresh_orphan = orphan.with_name("e" * 32)
    fresh_orphan.write_bytes(b"an upload in flight")
    outside = Path(media_root) / "other.txt"
    outside.write_bytes(b"not ours")
    _backdate(orphan, 8)
    _backdate(outside, 30)
    # Within the window only the old orphan goes; the retired file and the recent orphan stay.
    assert attachments.sweep() == {"retired_removed": 0, "orphans_removed": 1, "rows_forgotten": 0}
    assert stored_path(media_root, gone).exists() and not orphan.exists() and fresh_orphan.exists()
    later = timezone.now() + timedelta(days=8)
    assert attachments.sweep(now=later) == {"retired_removed": 1, "orphans_removed": 1, "rows_forgotten": 1}
    assert not stored_path(media_root, gone).exists() and not RetiredFile.objects.exists()
    assert not fresh_orphan.exists()
    assert stored_path(media_root, kept).exists() and outside.exists()  # in use, or outside attachments/
    # Idempotent.
    assert attachments.sweep(now=later) == {"retired_removed": 0, "orphans_removed": 0, "rows_forgotten": 0}
    assert stored_path(media_root, kept).exists()


def test_the_sweep_never_removes_a_file_in_use_and_forgets_its_retirement(a, api, media_root):
    made = upload(api(a["users"]["editor"]), f"applications/{a['app'].id}/attachments", "n.txt", b"x").json_body
    row = Attachment.objects.get(id=made["id"])
    RetiredFile.objects.create(
        organization=a["org"], name=row.storage_name, retired_at=timezone.now() - timedelta(days=30)
    )
    _backdate(stored_path(media_root, row), 30)
    attachments.sweep(now=timezone.now() + timedelta(days=30))
    assert stored_path(media_root, row).exists() and not RetiredFile.objects.exists()


def test_the_sweep_runs_nightly_and_from_the_command_line(a, api, media_root, settings):
    definition = jobs.SCHEDULES["attachments.sweep"]
    assert definition.cron == "20 3 * * *" and definition.func is attachments.scheduled_sweep
    settings.ATTACHMENT_RETENTION_DAYS = 1
    editor = api(a["users"]["editor"])
    made = upload(editor, f"applications/{a['app'].id}/attachments", "n.txt", b"x").json_body
    name = Attachment.objects.get(id=made["id"]).storage_name
    editor.delete(f"attachments/{made['id']}")
    RetiredFile.objects.filter(name=name).update(retired_at=timezone.now() - timedelta(days=2))
    out = io.StringIO()
    call_command("sweep_attachments", stdout=out)
    assert "1 retired" in out.getvalue() and not (Path(media_root) / name).exists()
    assert definition.func(timezone.now())["retired_removed"] == 0  # the schedule's callable, again: nothing left


def test_verify_reports_missing_and_changed_files(a, b, api, media_root):
    editor = api(a["users"]["editor"])
    path = f"applications/{a['app'].id}/attachments"
    one = Attachment.objects.get(id=upload(editor, path, "one.txt", b"one").json_body["id"])
    two = Attachment.objects.get(id=upload(editor, path, "two.txt", b"two").json_body["id"])
    three = Attachment.objects.get(id=upload(editor, path, "three.txt", b"three").json_body["id"])
    editor.post(path, {"url": "https://example.test"})  # links have no file
    upload(api(b["users"]["editor"]), f"applications/{b['app'].id}/attachments", "b.txt", b"b")
    out = io.StringIO()
    call_command("verify_attachments", stdout=out)
    assert "All 4 attachment files" in out.getvalue()
    stored_path(media_root, one).unlink()
    stored_path(media_root, two).write_bytes(b"owt")  # same size, other content
    stored_path(media_root, three).write_bytes(b"3")
    err = io.StringIO()
    with pytest.raises(CommandError, match="3 of 4"):
        call_command("verify_attachments", stderr=err)
    report = err.getvalue()
    assert f"attachment {one.id}" in report and "missing" in report
    assert f"attachment {two.id}" in report and "SHA-256" in report
    assert f"attachment {three.id}" in report and "size 1 bytes, recorded 5" in report
    with pytest.raises(CommandError, match="3 of 3"):
        call_command("verify_attachments", "--organization", a["org"].slug, stderr=io.StringIO())
    call_command("verify_attachments", "--organization", b["org"].slug, stdout=io.StringIO())
    with pytest.raises(CommandError, match="No organization"):
        call_command("verify_attachments", "--organization", "nope")


# ---- tenancy -----------------------------------------------------------------------------------------------------------


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()  # the composite keys are deferred; force the check


def test_the_database_keeps_attachments_in_their_parents_organization(a, b):
    link = {"kind": "link", "title": "x", "url": "https://example.test"}
    _rejected(lambda: Attachment.objects.create(organization=a["org"], resource=b["app"], **link))
    other_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    _rejected(lambda: Attachment.objects.create(organization=a["org"], vendor=other_vendor, **link))
    vendor = Vendor.objects.create(organization=a["org"], name="Ours")
    _rejected(lambda: Attachment.objects.create(organization=a["org"], resource=a["app"], vendor=vendor, **link))
    _rejected(lambda: Attachment.objects.create(organization=a["org"], **link))  # no parent
    _rejected(lambda: Attachment.objects.create(organization=a["org"], resource=a["app"], kind="file", title="x"))
    _rejected(
        lambda: Attachment.objects.create(
            organization=a["org"], resource=a["app"], storage_name="attachments/x", **link
        )
    )


def test_services_scope_to_the_organization_given(a, b):
    with pytest.raises(TypeError):
        attachments.parent_fields(a["org"])
    made = attachments.add_link(a["org"], a["users"]["editor"], a["app"], {"url": "https://example.test"})
    assert made.organization_id == a["org"].id
    _rejected(lambda: attachments.add_link(b["org"], b["users"]["editor"], a["app"], {"url": "https://example.test"}))


# ---- query counts ------------------------------------------------------------------------------------------------------


def test_the_list_query_count_does_not_grow_with_rows(a, api):
    viewer = api(a["users"]["viewer"])
    path = f"/api/v1/applications/{a['app'].id}/attachments"
    attachments.add_link(a["org"], a["users"]["editor"], a["app"], {"url": "https://example.test/0"})
    viewer.client.get(path)

    def count():
        with CaptureQueriesContext(connection) as queries:
            response = viewer.client.get(path)
        assert response.status_code == 200
        return len(queries), len(response.json())

    one_queries, one_rows = count()
    for i in range(1, 12):
        attachments.add_link(
            a["org"], a["users"]["admin" if i % 2 else "editor"], a["app"], {"url": f"https://e.test/{i}"}
        )
    many_queries, many_rows = count()
    assert (one_rows, many_rows) == (1, 12)
    assert many_queries == one_queries <= 8, (one_queries, many_queries)


# ---- the acceptance check ---------------------------------------------------------------------------------------------


def test_an_editor_documents_a_copilot_studio_agent_and_every_edit_is_in_its_history(a, api):
    """An editor creates a Copilot Studio agent with models, channels, owners and a custom "Business case" field, adds
    a SharePoint link and an uploaded PDF, and the agent's History shows each with a diff."""
    customfields.create_field(
        a["org"],
        a["users"]["admin"],
        {"name": "business_case", "label": "Business case", "type": "longtext", "applies_to": ["ai_agent"]},
    )
    prov = provider(a)
    models = {"large": model(a, prov, "Fabrikam Large", version="2"), "vision": model(a, prov, "Fabrikam Vision")}
    editor = api(a["users"]["editor"])
    created = editor.post(
        "ai-agents", complete(a, models, custom_field_data={"business_case": "Cuts HR ticket volume by a third."})
    )
    assert created.status_code == 201, created.json_body
    agent_id = created.json_body["id"]
    assert created.json_body["custom_field_data"]["business_case"] == "Cuts HR ticket volume by a third."
    link = editor.post(
        f"ai-agents/{agent_id}/attachments",
        {
            "url": "https://contoso.sharepoint.com/sites/HR/Shared%20Documents/HR%20Assistant%20design.docx",
            "title": "Design (SharePoint)",
            "category": "architecture",
        },
    )
    assert link.status_code == 201, link.json_body
    pdf = upload(editor, f"ai-agents/{agent_id}/attachments", "DPIA.pdf", PDF, category="security_review")
    assert pdf.status_code == 201, pdf.json_body
    listed = api(a["users"]["viewer"]).get(f"ai-agents/{agent_id}/attachments").json_body
    assert [(x["title"], x["kind"]) for x in listed] == [("Design (SharePoint)", "link"), ("DPIA.pdf", "file")]
    history = editor.get(f"ai-agents/{agent_id}/audit").json_body["results"]
    assert [h["action"] for h in history][:3] == ["attachment.created", "attachment.created", "resource.created"]
    for row in history[:3]:
        assert row["target"]["id"] == agent_id and row["target"]["kind"] == "ai_agent"
        diff = editor.get(f"ai-agents/{agent_id}/audit/{row['id']}").json_body["diff"]
        assert diff, row["action"]
    created_diff = {
        r["field"]: r for r in editor.get(f"ai-agents/{agent_id}/audit/{history[2]['id']}").json_body["diff"]
    }
    assert created_diff["platform"]["after"] == "copilot_studio"
    assert created_diff["custom_field_data.business_case"]["label"] == "Business case"
    pdf_diff = {r["field"]: r for r in editor.get(f"ai-agents/{agent_id}/audit/{history[0]['id']}").json_body["diff"]}
    assert pdf_diff["filename"]["after"] == "DPIA.pdf" and pdf_diff["content_type"]["after"] == "application/pdf"
    link_diff = {r["field"]: r for r in editor.get(f"ai-agents/{agent_id}/audit/{history[1]['id']}").json_body["diff"]}
    assert link_diff["url"]["after"].startswith("https://contoso.sharepoint.com/")
    assert api(a["users"]["viewer"]).client.get(pdf.json_body["download_url"]).status_code == 200


# ---- review follow-ups -----------------------------------------------------------------------------------------------


def test_a_link_with_a_credential_says_why(a, api):
    response = api(a["users"]["editor"]).post(
        f"applications/{a['app'].id}/attachments", {"url": "https://user:pw@example.test/doc"}
    )
    assert response.json_body["errors"]["url"] == ["Leave the user name and password out of the address."]


def test_a_link_refuses_keys_it_does_not_take(a, api):
    response = api(a["users"]["editor"]).post(
        f"applications/{a['app'].id}/attachments",
        {"url": "https://example.test", "sha256": "0" * 64, "storage_name": "attachments/x", "size": 1},
    )
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"sha256", "storage_name", "size"}
    assert not Attachment.objects.exists()


def zip_with_entries(count):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(count):
            archive.writestr(f"f{i}", b"")
    return buffer.getvalue()


def test_a_zip_with_too_many_entries_is_refused_before_it_is_opened(a, api, monkeypatch):
    path = f"applications/{a['app'].id}/attachments"
    editor = api(a["users"]["editor"])
    assert upload(editor, path, "fine.zip", zip_with_entries(attachments.ZIP_MAX_ENTRIES)).status_code == 201
    many = zip_with_entries(attachments.ZIP_MAX_ENTRIES + 1)
    opened: list[tuple] = []
    real = zipfile.ZipFile

    def spy(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", spy)
    too_many = upload(editor, path, "many.zip", many)
    assert too_many.status_code == 400 and "too many files" in too_many.json_body["errors"]["file"][0]
    # An end record claiming a central directory over 4 MB (a few entries, huge names) is refused the same way.
    eocd = attachments.EOCD + b"\x00" * 6 + (3).to_bytes(2, "little") + (5 * attachments.MB).to_bytes(4, "little")
    huge = b"PK\x03\x04" + b"\x00" * 26 + eocd + b"\x00" * 6
    assert attachments.zip_directory(io.BytesIO(huge)) == (3, 5 * attachments.MB)
    assert upload(editor, path, "huge.docx", huge).status_code == 400
    # ZIP64 placeholders read as over the limits.
    zip64 = b"PK\x03\x04" + attachments.EOCD + b"\x00" * 6 + b"\xff\xff" + b"\xff\xff\xff\xff" + b"\x00" * 6
    assert upload(editor, path, "z64.zip", zip64).status_code == 400
    assert opened == []
    assert attachments.zip_directory(io.BytesIO(b"PK\x03\x04 no end record")) is None


def test_a_zip64_locator_is_over_the_limit_and_zipfile_never_runs(a, api, monkeypatch):
    # The classic end record says 1 entry, the ZIP64 record (which zipfile follows) says 200,000.
    zip64_end = b"PK\x06\x06" + b"\x00" * 52
    locator = attachments.ZIP64_LOCATOR + b"\x00" * 16  # 20 bytes
    classic = attachments.EOCD + b"\x00" * 4 + (1).to_bytes(2, "little") * 2 + b"\x00" * 8 + b"\x00\x00"
    crafted = b"PK\x03\x04" + b"\x00" * 26 + zip64_end + locator + classic
    assert attachments.zip_directory(io.BytesIO(crafted)) == attachments.ZIP_OVER_LIMIT
    assert attachments.zip_directory(io.BytesIO(locator + classic)) == attachments.ZIP_OVER_LIMIT  # near the start

    def refuse(*args, **kwargs):
        raise AssertionError("zipfile.ZipFile must not be called")

    monkeypatch.setattr(zipfile, "ZipFile", refuse)
    path = f"applications/{a['app'].id}/attachments"
    refused = upload(api(a["users"]["editor"]), path, "z64.zip", crafted)
    assert refused.status_code == 400 and "ZIP64 archives aren't accepted" in refused.json_body["errors"]["file"][0]
    from catalog_code import bundle

    with pytest.raises(bundle.BundleRejected, match="ZIP64 archives aren't accepted"):
        bundle.read_zip(io.BytesIO(crafted))


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_uploads_cannot_both_pass_the_quota(settings, monkeypatch, media_root):
    """Each upload fits the quota alone; together they don't. The per-organization advisory lock makes the second
    wait for the first to commit, then count its file. used_bytes is slowed so, without the lock, both would read
    the same total and both commit."""
    import threading

    from tests.conftest import make_org

    f = make_org("quotarace")
    org, editor = f["org"], f["users"]["editor"]
    settings.ATTACHMENT_QUOTA_MB = 1
    real_used = attachments.used_bytes

    def slow_used(o):
        value = real_used(o)
        time.sleep(0.5)
        return value

    monkeypatch.setattr(attachments, "used_bytes", slow_used)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def uploader(name):
        try:
            start.wait(5)
            attachments.upload_file(org, editor, f["app"], SimpleUploadedFile(name, b"a" * 600_000), {})
            outcomes.append("ok")
        except Exception as e:  # FieldErrors for the one past the quota
            outcomes.append("quota" if "quota" in str(getattr(e, "errors", e)) else repr(e))
        finally:
            connection.close()

    threads = [threading.Thread(target=uploader, args=(f"{n}.txt",)) for n in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(outcomes) == ["ok", "quota"], outcomes
    assert Attachment.objects.filter(organization=org).count() == 1
