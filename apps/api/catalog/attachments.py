"""Attachments: files and links on records and vendors. Every write goes through the services here, which validate, scope to the
organization and audit in one transaction; the event's related target is the parent, so the record's History
shows it.

Storage. A file is saved through Django's default storage (MEDIA_ROOT, the `softwarden-media` volume) as
`attachments/<organization id>/<32 random hex digits>`: nothing of the original name, so a path can't be guessed, and
no URL route serves MEDIA_ROOT. It is read back only through the role-checked download endpoint.

Limits. The installation sets the largest file, the extensions and a per-organization quota (settings.py,
SOFTWARDEN_ATTACHMENT_*); an organization's Admin may tighten each (Organization.settings keys in ORG_KEYS), never
loosen it.

Content checks. The client's content type is ignored. The extension must be allowed, and the content must be what the
extension says (CHECKS): a signature for binary formats, the expected part for Office Open XML packages, UTF-8 with no
NUL for text. Only extensions with a check can be allowed at all, so HTML, SVG, scripts and programs never can be.
Downloads are always `attachment` with `nosniff`, a sandboxing CSP and `no-store`, never shown inline.

Deleting. A deleted attachment's file is not removed at once: a RetiredFile row keeps it for the retention window,
so a backup taken meanwhile still matches its database dump and a rolled-back delete never loses a file. The nightly
`attachments.sweep` schedule then deletes retired files past the window, and files no row references at all (an upload
whose transaction rolled back) once they are older than the window. `manage.py verify_attachments` checks that every
stored file exists and matches its size and SHA-256."""

import codecs
import hashlib
import logging
import os
import re
import unicodedata
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import URLValidator
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from core import limits as org_limits
from core.problems import FieldErrors

from .models import Attachment, AttachmentCategory, AttachmentKind, Resource, RetiredFile, Vendor
from .services import audit

log = logging.getLogger("softwarden.attachments")

ROOT = "attachments"
# Multipart framing around a file (boundaries, the title and category parts) on top of the size limit.
MULTIPART_FRAMING = 64 * 1024
MB = 1024 * 1024
TITLE_MAX = 200
FILENAME_MAX = 200
URL_MAX = 2000
CHUNK = 64 * 1024

# ---- content checks -----------------------------------------------------------------------------------------------

PDF = "pdf"
OOXML = "ooxml"
ZIP = "zip"
TEXT = "text"


@dataclass(frozen=True)
class Check:
    content_type: str
    how: str  # "signature", OOXML, ZIP or TEXT
    signatures: tuple[bytes, ...] = ()
    part: str = ""  # an Office Open XML package's main folder


OLE = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)  # legacy .doc / .xls
CHECKS: dict[str, Check] = {
    "pdf": Check("application/pdf", PDF),
    "docx": Check("application/vnd.openxmlformats-officedocument.wordprocessingml.document", OOXML, part="word/"),
    "xlsx": Check("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", OOXML, part="xl/"),
    "pptx": Check("application/vnd.openxmlformats-officedocument.presentationml.presentation", OOXML, part="ppt/"),
    "vsdx": Check("application/vnd.ms-visio.drawing", OOXML, part="visio/"),
    "doc": Check("application/msword", "signature", OLE),
    "xls": Check("application/vnd.ms-excel", "signature", OLE),
    "png": Check("image/png", "signature", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": Check("image/jpeg", "signature", (b"\xff\xd8\xff",)),
    "jpeg": Check("image/jpeg", "signature", (b"\xff\xd8\xff",)),
    "gif": Check("image/gif", "signature", (b"GIF87a", b"GIF89a")),
    "zip": Check("application/zip", ZIP),
    "txt": Check("text/plain; charset=utf-8", TEXT),
    "csv": Check("text/csv; charset=utf-8", TEXT),
    "md": Check("text/markdown; charset=utf-8", TEXT),
    "json": Check("application/json", TEXT),
}
# The extensions an installation may allow: those with a content check. Anything else (html, svg, js, exe, ...) can't
# be configured (catalog/apps.py system check softwarden.E101).
KNOWN_EXTENSIONS = frozenset(CHECKS)


def extension_of(filename: str) -> str:
    _, ext = os.path.splitext(filename)
    return ext.lower().lstrip(".")


def clean_filename(name: str) -> str:
    """The name to keep and offer on download: the last path part, without control or format characters, at most
    FILENAME_MAX characters with the extension kept."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C")
    base = " ".join(base.split()).strip(". ")
    if len(base) > FILENAME_MAX:
        stem, ext = os.path.splitext(base)
        base = stem[: FILENAME_MAX - len(ext)].rstrip(". ") + ext
    return base


@dataclass
class Scanned:
    size: int
    sha256: str
    content_type: str


def _refuse(message: str) -> NoReturn:
    raise FieldErrors({"file": [message]})


def scan(upload, ext: str, max_bytes: int) -> Scanned:
    """Read the upload once in chunks: count its size (refused as soon as it passes max_bytes), hash it, and check its
    content against its extension. Raises FieldErrors keyed `file`."""
    check = CHECKS[ext]
    digest = hashlib.sha256()
    size = 0
    head = b""
    decoder = codecs.getincrementaldecoder("utf-8")("strict") if check.how == TEXT else None
    for chunk in upload.chunks(CHUNK):
        size += len(chunk)
        if size > max_bytes:
            _refuse(f"The file is larger than {max_bytes // MB} MB.")
        digest.update(chunk)
        if len(head) < 8192:
            head += chunk[: 8192 - len(head)]
        if decoder is not None:
            if b"\x00" in chunk:
                _refuse(f"A .{ext} file must be text, but this one holds binary data.")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                _refuse(f"A .{ext} file must be UTF-8 text.")
    if size == 0:
        _refuse("The file is empty.")
    if decoder is not None:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            _refuse(f"A .{ext} file must be UTF-8 text.")
    elif check.how == PDF:
        if b"%PDF-" not in head[:1024]:
            _refuse("The file's content isn't a PDF.")
    elif check.how == "signature":
        if not head.startswith(check.signatures):
            _refuse(f"The file's content doesn't match its .{ext} extension.")
    else:
        _check_package(upload, ext, check, head)
    return Scanned(size=size, sha256=digest.hexdigest(), content_type=check.content_type)


# zipfile reads a ZIP's whole central directory into memory, one object per entry; these bound it before it is opened.
ZIP_MAX_ENTRIES = 10_000
ZIP_MAX_DIRECTORY = 4 * MB
EOCD = b"PK\x05\x06"
EOCD_SEARCH = 22 + 65535 + 20  # the end record is 22 bytes plus a comment of up to 64 KB; a ZIP64 locator is 20 more
ZIP64_LOCATOR = b"PK\x06\x07"
ZIP_OVER_LIMIT = (2**64, 2**64)


def zip_directory(upload) -> tuple[int, int] | None:
    """(entries, central directory bytes) from the end-of-central-directory record, or None when there is none.
    A ZIP64 archive reads as over any limit: zipfile follows the ZIP64 locator to a record whose counts replace the
    classic ones, so the classic record's numbers can't be trusted (a crafted file says 1 entry there and 200,000 in
    the ZIP64 record). Refusing is the stricter choice and costs nothing: no upload here is anywhere near 4 GB or
    65,535 entries, the only archives that need ZIP64."""
    upload.seek(0, os.SEEK_END)
    end = upload.tell()
    start = max(0, end - EOCD_SEARCH)
    upload.seek(start)
    tail = upload.read(end - start)
    at = tail.rfind(EOCD)
    if at < 0 or len(tail) - at < 22:
        return None
    if at >= 20 and tail[at - 20 : at - 16] == ZIP64_LOCATOR:
        return ZIP_OVER_LIMIT
    entries = int.from_bytes(tail[at + 10 : at + 12], "little")
    directory = int.from_bytes(tail[at + 12 : at + 16], "little")
    return entries, directory


def _check_package(upload, ext: str, check: Check, head: bytes) -> None:
    """A ZIP archive, and for an Office Open XML type a package with its content types and main folder. Reads only
    the archive's directory, after its end record shows the directory is a sane size; nothing is extracted."""
    if not head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        _refuse(f"The file's content doesn't match its .{ext} extension.")
    try:
        found = zip_directory(upload)
    finally:
        upload.seek(0)
    if found is None:
        _refuse(f"The .{ext} file is damaged or isn't a ZIP archive.")
    if found == ZIP_OVER_LIMIT:
        _refuse(f"The .{ext} file is a ZIP64 archive; ZIP64 archives aren't accepted.")
    entries, directory = found
    if entries > ZIP_MAX_ENTRIES or directory > ZIP_MAX_DIRECTORY:
        _refuse(f"The .{ext} archive lists too many files (at most {ZIP_MAX_ENTRIES:,}).")
    try:
        upload.seek(0)
        with zipfile.ZipFile(upload) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        _refuse(f"The .{ext} file is damaged or isn't a ZIP archive.")
    finally:
        upload.seek(0)
    if check.how == OOXML and not ("[Content_Types].xml" in names and any(n.startswith(check.part) for n in names)):
        _refuse(f"The file's content doesn't match its .{ext} extension.")


# ---- limits -------------------------------------------------------------------------------------------------------

# Organization.settings keys an Admin sets through PATCH /organization (catalog/admin_services.update_organization);
# audited (catalog/snapshots.ORG_SETTINGS_AUDITED). Missing means the installation's value.
MAX_SIZE_KEY = "attachment_max_size_mb"
EXTENSIONS_KEY = "attachment_extensions"
QUOTA_KEY = "attachment_quota_mb"
ORG_KEYS = (MAX_SIZE_KEY, EXTENSIONS_KEY, QUOTA_KEY)


@dataclass(frozen=True)
class Limits:
    max_size_mb: int
    extensions: tuple[str, ...]
    quota_mb: int | None  # None: no quota


def installation_limits() -> Limits:
    quota = settings.ATTACHMENT_QUOTA_MB
    return Limits(
        max_size_mb=settings.ATTACHMENT_MAX_SIZE_MB,
        extensions=tuple(dict.fromkeys(settings.ATTACHMENT_EXTENSIONS)),
        quota_mb=quota if quota > 0 else None,
    )


def limits_for(org) -> Limits:
    """The organization's limits: its own values where set, each held within the installation's."""
    base = installation_limits()
    own = org.settings or {}
    max_mb = own.get(MAX_SIZE_KEY)
    exts = own.get(EXTENSIONS_KEY)
    quota = own.get(QUOTA_KEY)
    return Limits(
        max_size_mb=min(max_mb, base.max_size_mb) if isinstance(max_mb, int) else base.max_size_mb,
        extensions=tuple(e for e in base.extensions if e in exts) if isinstance(exts, list) else base.extensions,
        quota_mb=(min(quota, base.quota_mb) if base.quota_mb else quota) if isinstance(quota, int) else base.quota_mb,
    )


def used_bytes(org) -> int:
    return Attachment.objects.filter(organization=org, kind=AttachmentKind.FILE).aggregate(n=Sum("size"))["n"] or 0


def clean_limit_settings(data: dict[str, Any], errors: dict[str, list[str]]) -> dict[str, Any]:
    """{key: value or None} for the attachment keys present in an organization PATCH; None removes the key (back to
    the installation's value). Errors are added to `errors`."""
    base = installation_limits()
    out: dict[str, Any] = {}
    if MAX_SIZE_KEY in data:
        value = data[MAX_SIZE_KEY]
        if value is None:
            out[MAX_SIZE_KEY] = None
        elif isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= base.max_size_mb:
            out[MAX_SIZE_KEY] = value
        else:
            errors[MAX_SIZE_KEY] = [f"Enter a whole number of MB from 1 to {base.max_size_mb}, or null."]
    if EXTENSIONS_KEY in data:
        value = data[EXTENSIONS_KEY]
        if value is None:
            out[EXTENSIONS_KEY] = None
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) for v in value)
            and {v.lower().lstrip(".") for v in value} <= set(base.extensions)
        ):
            wanted = {v.lower().lstrip(".") for v in value}
            out[EXTENSIONS_KEY] = [e for e in base.extensions if e in wanted]
        else:
            errors[EXTENSIONS_KEY] = [f"Choose one or more of: {', '.join(base.extensions)}; or null."]
    if QUOTA_KEY in data:
        value = data[QUOTA_KEY]
        ceiling = base.quota_mb
        if value is None:
            out[QUOTA_KEY] = None
        elif isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= (ceiling or 10**9):
            out[QUOTA_KEY] = value
        else:
            top = f"from 1 to {ceiling}" if ceiling else "of 1 or more"
            errors[QUOTA_KEY] = [f"Enter a whole number of MB {top}, or null."]
    return out


# ---- parents ------------------------------------------------------------------------------------------------------


def parent_fields(parent) -> dict[str, Any]:
    from money.models import Contract

    if isinstance(parent, Resource):
        return {"resource": parent}
    if isinstance(parent, Vendor):
        return {"vendor": parent}
    if isinstance(parent, Contract):
        return {"contract": parent}
    raise TypeError(f"Attachments can't belong to a {type(parent).__name__}.")


def parent_type(attachment) -> str:
    """application, component, integration, ai_agent, vendor or contract."""
    if attachment.resource_id is not None:
        return attachment.resource.kind
    return "contract" if attachment.contract_id is not None else "vendor"


# ---- writes -------------------------------------------------------------------------------------------------------


def _title(data, fallback: str, errors) -> str | None:
    if "title" not in data or data.get("title") in (None, ""):
        return fallback[:TITLE_MAX]
    value = data.get("title")
    if not isinstance(value, str) or not value.strip():
        errors["title"] = ["Enter a title."]
        return None
    value = " ".join(value.split())
    if len(value) > TITLE_MAX:
        errors["title"] = [f"Use {TITLE_MAX} characters or fewer."]
        return None
    return value


def _category(data, errors, default=AttachmentCategory.OTHER) -> str | None:
    if "category" not in data or data.get("category") in (None, ""):
        return default
    value = data.get("category")
    if value not in AttachmentCategory.values:
        errors["category"] = [f"Choose one of: {', '.join(AttachmentCategory.values)}."]
        return None
    return value


def _url(value, errors) -> str | None:
    url = value.strip() if isinstance(value, str) else ""
    if not url:
        errors["url"] = ["Enter the link's web address."]
        return None
    if len(url) > URL_MAX:
        errors["url"] = [f"Use {URL_MAX} characters or fewer."]
        return None
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        errors["url"] = ["Enter a full web address starting with http:// or https://."]
        return None
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:  # a credential in a link would be shown to everyone
        errors["url"] = ["Leave the user name and password out of the address."]
        return None
    return url


def audit_values(attachment) -> dict[str, Any]:
    """What an attachment event's payload names: never the stored file's name or its contents."""
    values: dict[str, Any] = {"kind": attachment.kind, "title": attachment.title, "category": attachment.category}
    if attachment.kind == AttachmentKind.FILE:
        values.update(filename=attachment.filename, size=attachment.size, content_type=attachment.content_type)
    else:
        values["url"] = attachment.url
    return values


def _lock_quota(org) -> None:
    """Serialise this organization's uploads for the quota check, until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"attachments:{org.pk}"])


def new_storage_name(org) -> str:
    return f"{ROOT}/{org.pk}/{uuid.uuid4().hex}"


@transaction.atomic
def upload_file(org, actor, parent, upload, data) -> Attachment:
    """A file attachment on `parent` (a record, a vendor or a contract of `org`, looked up by the caller in the
    organization).
    Validates the title and category, the name's extension against the organization's list, the size, the content,
    and the quota; then stores the file and audits `attachment.created`. A file stored by a transaction that later
    rolls back has no row and is removed by the sweep."""
    errors: dict[str, list[str]] = {}
    limits = limits_for(org)
    if upload is None:
        raise FieldErrors({"file": ["Choose a file to upload."]})
    filename = clean_filename(upload.name)
    ext = extension_of(filename)
    title = _title(data, filename, errors)
    category = _category(data, errors)
    if not filename or not ext:
        errors["file"] = ["The file needs a name with an extension, such as report.pdf."]
    elif ext not in limits.extensions:
        errors["file"] = [f"Files of type .{ext} can't be attached here. Allowed: {', '.join(limits.extensions)}."]
    if errors:
        raise FieldErrors(errors)
    max_bytes = limits.max_size_mb * MB
    if upload.size is not None and upload.size > max_bytes:
        _refuse(f"The file is larger than {limits.max_size_mb} MB.")
    scanned = scan(upload, ext, max_bytes)
    if limits.quota_mb is not None:
        _lock_quota(org)
        if used_bytes(org) + scanned.size > limits.quota_mb * MB:
            _refuse(f"This organization's attachments would pass its {limits.quota_mb} MB quota. Remove some first.")
    # The organization's attachment_bytes limit (core/limits.py), beside the Admin's own quota above.
    org_limits.check_room(org, org_limits.ATTACHMENT_BYTES, scanned.size)
    upload.seek(0)
    wanted = new_storage_name(org)
    stored = default_storage.save(wanted, upload)
    if stored != wanted:  # a random name never exists already; refuse rather than keep an unexpected path
        default_storage.delete(stored)
        raise RuntimeError("The attachment store renamed a new file.")
    attachment = Attachment(
        organization=org,
        **parent_fields(parent),
        kind=AttachmentKind.FILE,
        title=str(title),
        category=str(category),
        storage_name=stored,
        filename=filename,
        content_type=scanned.content_type,
        size=scanned.size,
        sha256=scanned.sha256,
        uploaded_by=actor,
    )
    attachment.save()
    audit(org, actor, "attachment.created", attachment, audit_values(attachment))
    return attachment


LINK_FIELDS = ("kind", "url", "title", "category")


@transaction.atomic
def add_link(org, actor, parent, data) -> Attachment:
    """A link attachment (an http or https address, such as a SharePoint document) on `parent`."""
    errors: dict[str, list[str]] = {}
    for key in data:
        if key not in LINK_FIELDS:
            errors[key] = ["A link takes only kind, url, title and category."]
    if data.get("kind") not in (None, "", AttachmentKind.LINK):
        errors["kind"] = ["Send a file as multipart/form-data; a JSON body adds a link."]
    url = _url(data.get("url"), errors)
    title = _title(data, url or "", errors)
    category = _category(data, errors)
    if errors:
        raise FieldErrors(errors)
    attachment = Attachment(
        organization=org,
        **parent_fields(parent),
        kind=AttachmentKind.LINK,
        title=str(title),
        category=str(category),
        url=str(url),
        uploaded_by=actor,
    )
    attachment.save()
    audit(org, actor, "attachment.created", attachment, audit_values(attachment))
    return attachment


EDITABLE = ("title", "category", "url")


@transaction.atomic
def update_attachment(org, actor, attachment, data) -> Attachment:
    """Change the title, the category, or a link's address. A file's content never changes: upload a new one."""
    errors: dict[str, list[str]] = {}
    unknown = [k for k in data if k not in EDITABLE]
    for key in unknown:
        errors[key] = ["This can't be changed; only title, category and a link's url can."]
    values: dict[str, Any] = {}
    if "title" in data:
        value = data.get("title")
        if not isinstance(value, str) or not value.strip():
            errors["title"] = ["Enter a title."]
        else:
            values["title"] = _title(data, "", errors)
    if "category" in data:
        if data.get("category") in (None, ""):
            errors["category"] = [f"Choose one of: {', '.join(AttachmentCategory.values)}."]
        else:
            values["category"] = _category(data, errors)
    if "url" in data:
        if attachment.kind != AttachmentKind.LINK:
            errors["url"] = ["A file has no web address; upload a new file instead."]
        else:
            values["url"] = _url(data.get("url"), errors)
    if errors:
        raise FieldErrors(errors)
    changes = {k: [getattr(attachment, k), v] for k, v in values.items() if getattr(attachment, k) != v}
    for key, value in values.items():
        setattr(attachment, key, value)
    if changes:  # an edit that changes nothing writes nothing
        attachment.save()
        audit(org, actor, "attachment.updated", attachment, {"title": attachment.title, **changes})
    return attachment


def _retire(attachment) -> None:
    if attachment.kind == AttachmentKind.FILE and attachment.storage_name:
        RetiredFile.objects.get_or_create(
            name=attachment.storage_name,
            defaults={
                "organization_id": attachment.organization_id,
                "sha256": attachment.sha256,
                "size": attachment.size,
            },
        )


@transaction.atomic
def delete_attachment(org, actor, attachment) -> None:
    """Remove an attachment. Its file is retired, not deleted: the sweep removes it after the retention window."""
    audit(org, actor, "attachment.deleted", attachment, audit_values(attachment))
    _retire(attachment)
    attachment.delete()


def remove_for(org, actor, parent) -> int:
    """Remove every attachment of a record, vendor or contract being deleted (inside its delete's transaction), each
    audited and its file retired. Returns how many."""
    rows = list(Attachment.objects.filter(organization=org, **parent_fields(parent)).select_related("resource"))
    for attachment in rows:
        delete_attachment(org, actor, attachment)
    return len(rows)


# ---- reading files ------------------------------------------------------------------------------------------------


def open_file(attachment):
    """The stored file opened for reading, or None when it is missing from storage."""
    try:
        return default_storage.open(attachment.storage_name, "rb")
    except FileNotFoundError:
        log.warning("attachment file missing from storage attachment=%s", attachment.pk)
        return None


_TOKEN_UNSAFE = re.compile(r'[^\x20-\x7e]|["\\%;]')


def content_disposition(filename: str) -> str:
    """`attachment` with an ASCII fallback name and the exact name in RFC 5987 encoding (RFC 6266 section 4.3)."""
    from urllib.parse import quote

    name = clean_filename(filename) or "download"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = _TOKEN_UNSAFE.sub("_", ascii_name).strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


# ---- sweep and verify ---------------------------------------------------------------------------------------------


def _walk(storage, path: str) -> Iterator[str]:
    try:
        dirs, files = storage.listdir(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    for name in files:
        yield f"{path}/{name}"
    for directory in dirs:
        yield from _walk(storage, f"{path}/{directory}")


def sweep(now: datetime | None = None, storage=None) -> dict[str, int]:
    """Delete stored files no attachment references once past the retention window: retired files by their retirement
    time, files with no row at all (an upload whose transaction rolled back) by their modification time. Safe to run
    any number of times; a file an attachment references is never deleted, and its RetiredFile row (left by a restore)
    is dropped. Installation-wide: the `attachments.sweep` schedule (nightly) and `manage.py sweep_attachments`."""
    storage = storage or default_storage
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.ATTACHMENT_RETENTION_DAYS)
    referenced = set(Attachment.objects.exclude(storage_name="").values_list("storage_name", flat=True))
    retired = dict(RetiredFile.objects.values_list("name", "retired_at"))
    removed_retired = removed_orphans = 0
    for name in _walk(storage, ROOT):
        if name in referenced:
            continue
        since = retired.get(name)
        if since is None:
            try:
                since = storage.get_modified_time(name)
            except FileNotFoundError:
                continue
        if since > cutoff:
            continue
        storage.delete(name)
        if name in retired:
            removed_retired += 1
        else:
            removed_orphans += 1
    # Rows whose file is gone (deleted above or earlier) past the window, and rows for a file in use again.
    forgotten, _ = RetiredFile.objects.filter(retired_at__lte=cutoff).exclude(name__in=referenced).delete()
    stale = [name for name in retired if name in referenced]
    if stale:
        RetiredFile.objects.filter(name__in=stale).delete()
    if removed_retired or removed_orphans:
        log.info("attachment sweep removed %s retired and %s orphaned file(s)", removed_retired, removed_orphans)
    return {"retired_removed": removed_retired, "orphans_removed": removed_orphans, "rows_forgotten": forgotten}


def scheduled_sweep(now):
    return sweep(now)


def verify(organization=None, storage=None) -> tuple[int, list[str]]:
    """(files checked, problems): every file attachment's stored file must exist and match its size and SHA-256."""
    storage = storage or default_storage
    rows = Attachment.objects.filter(kind=AttachmentKind.FILE)
    if organization is not None:
        rows = rows.filter(organization=organization)
    problems: list[str] = []
    checked = 0
    for att in rows.only("pk", "organization_id", "storage_name", "size", "sha256").order_by("pk").iterator():
        checked += 1
        where = f"attachment {att.pk} (organization {att.organization_id})"
        try:
            handle = storage.open(att.storage_name, "rb")
        except FileNotFoundError:
            problems.append(f"{where}: the stored file is missing")
            continue
        digest = hashlib.sha256()
        size = 0
        with handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        if size != att.size:
            problems.append(f"{where}: size {size} bytes, recorded {att.size}")
        elif digest.hexdigest() != att.sha256:
            problems.append(f"{where}: SHA-256 doesn't match the recorded one")
    return checked, problems


def check_settings(app_configs=None, **kwargs):
    """Django system check (catalog/apps.py): the installation's attachment settings are usable."""
    from django.core.checks import Error

    errors = []
    unknown = sorted({e for e in settings.ATTACHMENT_EXTENSIONS if e not in KNOWN_EXTENSIONS})
    if unknown:
        errors.append(
            Error(
                f"SOFTWARDEN_ATTACHMENT_EXTENSIONS lists {', '.join(unknown)}, which have no content check.",
                hint=f"Use only: {', '.join(sorted(KNOWN_EXTENSIONS))}.",
                id="softwarden.E101",
            )
        )
    if not settings.ATTACHMENT_EXTENSIONS:
        errors.append(Error("SOFTWARDEN_ATTACHMENT_EXTENSIONS is empty.", id="softwarden.E102"))
    if settings.ATTACHMENT_MAX_SIZE_MB < 1 or settings.ATTACHMENT_QUOTA_MB < 0:
        errors.append(
            Error(
                "SOFTWARDEN_ATTACHMENT_MAX_MB must be 1 or more and SOFTWARDEN_ATTACHMENT_QUOTA_MB 0 or more.",
                id="softwarden.E103",
            )
        )
    from imports.csvio import MAX_BYTES as CSV_IMPORT_MAX_BYTES  # the request limit must fit a CSV import too

    needed = max(settings.ATTACHMENT_MAX_SIZE_MB * MB, CSV_IMPORT_MAX_BYTES) + MULTIPART_FRAMING
    if settings.MAX_REQUEST_MB * MB < needed:
        errors.append(
            Error(
                f"SOFTWARDEN_MAX_REQUEST_MB ({settings.MAX_REQUEST_MB}) is too small for the attachment limit "
                f"({settings.ATTACHMENT_MAX_SIZE_MB} MB) or a 5 MB CSV import, plus the multipart framing.",
                hint="Set it to at least the attachment limit plus 1 (Compose gives Caddy the same value).",
                id="softwarden.E105",
            )
        )
    if settings.ATTACHMENT_RETENTION_DAYS < 1:
        errors.append(Error("SOFTWARDEN_ATTACHMENT_RETENTION_DAYS must be 1 or more.", id="softwarden.E104"))
    return errors
