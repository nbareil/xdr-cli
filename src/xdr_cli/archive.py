"""Extraction of the archives returned by XDR file retrievals."""

import json
import logging
import os
import pathlib
import shutil
import subprocess
import zipfile
from typing import List, Optional

from .errors import XdrValidationError

logger = logging.getLogger(__name__)

# Fixed by XDR for every file retrieval archive, so it is no secret.
RETRIEVAL_PASSWORD = b"suspicious"
# Some agents (seen on Windows) nest the retrieved files in this archive.
INNER_ARCHIVE = "script_output.zip"


_S_IFMT = 0o170000
_S_IFLNK = 0o120000


def check_zip_members(zf: zipfile.ZipFile, output_dir: pathlib.Path) -> List[zipfile.ZipInfo]:
    """Return the file members of ``zf``, refusing any that would land
    outside ``output_dir``. Symlinks are refused too: an external extractor
    would create them, and a later member could then be written through one."""
    root = str(output_dir.resolve())
    members = []
    for member in zf.infolist():
        name = member.filename
        if not name or name.endswith("/"):
            continue
        destination = (output_dir / name).resolve()
        if os.path.commonpath([root, str(destination)]) != root:
            raise XdrValidationError(f"Unsafe zip entry path: {name}")
        if (member.external_attr >> 16) & _S_IFMT == _S_IFLNK:
            raise XdrValidationError(f"Unsafe zip entry (symlink): {name}")
        members.append(member)
    return members


def external_unzip_command(zip_path: pathlib.Path, output_dir: pathlib.Path, password: bytes) -> Optional[List[str]]:
    """Command line of the first native extractor found, or None.

    The retrieval password is a fixed, public value, so passing it on the
    command line leaks nothing."""
    pwd = password.decode()
    for name in ("7zz", "7z", "7za"):
        exe = shutil.which(name)
        if exe:
            return [exe, "x", "-y", "-bd", "-bso0", "-bsp0", f"-p{pwd}", f"-o{output_dir}", str(zip_path)]
    exe = shutil.which("unzip")
    if exe:
        return [exe, "-q", "-o", "-P", pwd, str(zip_path), "-d", str(output_dir)]
    return None


def safe_extract_zip(zip_path: pathlib.Path, output_dir: pathlib.Path, password: bytes):
    """Extract a retrieval archive into ``output_dir``.

    XDR encrypts retrieval archives with ZipCrypto, which the stdlib decrypts
    in pure Python at about 1 MB/s; 7-Zip and Info-ZIP unzip do it at disk
    speed. Use them when installed and fall back to zipfile otherwise."""
    with zipfile.ZipFile(zip_path) as zf:
        members = check_zip_members(zf, output_dir)
        cmd = external_unzip_command(zip_path, output_dir, password)
        if cmd:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if proc.returncode == 0:
                return
            logger.warning(
                "%s failed to extract %s (exit %d: %s); falling back to Python",
                os.path.basename(cmd[0]), zip_path, proc.returncode,
                (proc.stderr or proc.stdout).strip()[:300],
            )
        for member in members:
            destination = output_dir / member.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, pwd=password) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def extract_retrieval(archive: pathlib.Path, output_dir: pathlib.Path) -> None:
    """Extract a retrieval archive, including a nested script_output.zip."""
    safe_extract_zip(archive, output_dir, RETRIEVAL_PASSWORD)
    inner = output_dir / INNER_ARCHIVE
    if inner.is_file():
        safe_extract_zip(inner, output_dir, RETRIEVAL_PASSWORD)
        inner.unlink()


def retrieved_file(output_dir: pathlib.Path) -> pathlib.Path:
    """Return the single retrieved file in an extracted archive.

    The manifest maps each requested path to its place in the archive; the
    agent also adds files of its own (stdout.txt, script_output.txt)."""
    manifest = output_dir / "manifest.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("entries") or []
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        for info in entry.get("files") or []:
            archived = str(info.get("archivedPath") or "").replace("\\", "/")
            if archived and (output_dir / archived).is_file():
                return output_dir / archived
    files = [p for p in output_dir.rglob("*") if p.is_file() and p != manifest]
    if len(files) != 1:
        raise XdrValidationError("Expected one retrieved file, found %d: %s" % (len(files), files))
    return files[0]
