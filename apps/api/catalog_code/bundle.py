"""Reading and writing a catalog-as-code tree: a zip upload, a directory (the CLI), and the export zip.

A tree is {relative path: bytes} of its YAML files (.yaml, .yml). Everything else is ignored, as are hidden paths
(.git/, .github/), so a whole repository can be uploaded. What is refused, before any file is parsed (BundleRejected,
a 400 for the API):

- a zip over MAX_UPLOAD_BYTES, or whose directory lists more than MAX_ENTRIES entries or is over MAX_DIRECTORY_BYTES
  (read from the end record before zipfile loads it, as attachments do: catalog/attachments.zip_directory);
- a path that is absolute, has a drive letter, a backslash, a NUL, a "." or ".." part, or more than MAX_DEPTH parts;
- a symbolic link (zip mode bits or on disk), an encrypted entry, two paths that differ only in case;
- more than MAX_FILES YAML files, or more than MAX_TOTAL_BYTES of them once uncompressed (counted from the bytes
  actually inflated, never the sizes the zip claims).

A YAML file over fmt.MAX_FILE_BYTES is that file's error, not the tree's."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

from catalog.attachments import ZIP_OVER_LIMIT, zip_directory

from .fmt import MAX_FILE_BYTES

MB = 1024 * 1024
MAX_UPLOAD_BYTES = 16 * MB
MAX_ENTRIES = 25_000
MAX_DIRECTORY_BYTES = 8 * MB
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 16 * MB
MAX_DEPTH = 8
SUFFIXES = (".yaml", ".yml")
# A file too large to read gets this placeholder: the parser reports it as that file's error.
TOO_LARGE = b"\x00too-large"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # every export entry's timestamp, so the same tree gives the same zip bytes


class BundleRejected(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _parts(name: str) -> tuple[str, ...] | None:
    """The path's parts, or None for an entry to ignore (a folder, a hidden path, a file that isn't YAML). Raises
    BundleRejected for an unsafe path."""
    if "\\" in name or "\x00" in name or name.startswith("/") or ":" in name:
        raise BundleRejected(f"The archive has an unsafe path: {name[:200]!r}.")
    parts = tuple(p for p in name.split("/") if p != "")
    if any(p in (".", "..") for p in parts):
        raise BundleRejected(f"The archive has an unsafe path: {name[:200]!r}.")
    if not parts or name.endswith("/"):
        return None
    if len(parts) > MAX_DEPTH:
        raise BundleRejected(f"{name[:200]} is nested deeper than {MAX_DEPTH} folders.")
    if any(p.startswith(".") for p in parts) or not parts[-1].lower().endswith(SUFFIXES):
        return None
    return parts


class _Collector:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.total = 0

    def add(self, path: str, read) -> None:
        folded = path.lower()
        if folded in self.seen:
            raise BundleRejected(f"Two files are named {path} (paths are compared ignoring case).")
        self.seen.add(folded)
        if len(self.files) >= MAX_FILES:
            raise BundleRejected(f"The catalog has more than {MAX_FILES:,} YAML files.")
        data = read(MAX_FILE_BYTES + 1)
        self.total += len(data)  # what was inflated counts, an oversize file's bytes included
        if self.total > MAX_TOTAL_BYTES:
            raise BundleRejected(f"The YAML files add up to more than {MAX_TOTAL_BYTES // MB} MB.")
        self.files[path] = TOO_LARGE if len(data) > MAX_FILE_BYTES else data


def read_zip(upload) -> dict[str, bytes]:
    """The YAML files of an uploaded zip (a file-like object that can seek). Raises BundleRejected."""
    upload.seek(0, os.SEEK_END)
    size = upload.tell()
    upload.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise BundleRejected(f"The zip is larger than {MAX_UPLOAD_BYTES // MB} MB.")
    if not upload.read(4).startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise BundleRejected("Upload a zip archive.")
    found = zip_directory(upload)
    upload.seek(0)
    if found is None:
        raise BundleRejected("The zip is damaged.")
    if found == ZIP_OVER_LIMIT:
        raise BundleRejected("ZIP64 archives aren't accepted.")
    entries, directory = found
    if entries > MAX_ENTRIES or directory > MAX_DIRECTORY_BYTES:
        raise BundleRejected(f"The zip lists too many entries (at most {MAX_ENTRIES:,}).")
    out = _Collector()
    try:
        with zipfile.ZipFile(upload) as archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise BundleRejected(
                        f"The zip holds a symbolic link ({info.filename[:200]}); links aren't followed."
                    )
                parts = _parts(info.filename)
                if parts is None:
                    continue
                if info.flag_bits & 0x1:
                    raise BundleRejected(f"{info.filename[:200]} is encrypted.")
                with archive.open(info) as stream:
                    out.add("/".join(parts), stream.read)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, OSError, ValueError):
        raise BundleRejected("The zip is damaged or uses a compression this server can't read.") from None
    finally:
        upload.seek(0)
    return out.files


def read_dir(root: Path) -> dict[str, bytes]:
    """The YAML files under `root` (the CLI's --dir). Hidden folders (.git) are skipped; a symbolic link is refused."""
    root = Path(root)
    if not root.is_dir():
        raise BundleRejected(f"{root} isn't a directory.")
    out = _Collector()
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        for name in list(dirs):
            if name.startswith("."):
                dirs.remove(name)
            elif (here / name).is_symlink():
                raise BundleRejected(
                    f"{(here / name).relative_to(root).as_posix()} is a symbolic link; links aren't followed."
                )
        dirs.sort()
        for name in sorted(names):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise BundleRejected(f"{relative} is a symbolic link; links aren't followed.")
            parts = _parts(relative)
            if parts is None:
                continue
            with path.open("rb") as stream:
                out.add(relative, stream.read)
    return out.files


def write_zip(files: dict[str, bytes]) -> bytes:
    """The tree as a zip with fixed timestamps and modes, in path order: the same tree gives the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, files[path])
    return buffer.getvalue()


def write_dir(root: Path, files: dict[str, bytes], managed: list[str]) -> int:
    """Write the tree under `root`. YAML files left in the managed folders from an earlier export are removed first,
    so a record deleted since is deleted from the tree too; nothing else is touched (catalog.yaml is overwritten).
    Returns how many files were written."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    targets = {path: root / PurePosixPath(path) for path in files}
    # Nothing below the root is written through a link: not a kind folder, not a file an earlier tree left as a link
    # (it would write wherever the link points). Checked for every path before anything is written or removed.
    for path in [*managed, *files]:
        _refuse_links(root, root / PurePosixPath(path), path)
    for folder in managed:
        directory = root / folder
        if directory.is_dir():
            for old in directory.iterdir():
                if old.is_file() and not old.is_symlink() and old.name.lower().endswith(SUFFIXES):
                    old.unlink()
    for target, data in ((targets[p], files[p]) for p in files):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(files)


def _refuse_links(root: Path, target: Path, shown: str) -> None:
    """BundleRejected when `target`, or any folder between `root` and it, is a symbolic link."""
    current = target
    while current != root and current != current.parent:
        if current.is_symlink():
            raise BundleRejected(f"{shown} goes through a symbolic link; refusing to write through it.")
        current = current.parent
