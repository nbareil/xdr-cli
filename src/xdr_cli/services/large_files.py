"""Retrieve files larger than the get-file limit.

Two ways around the limit, both driven by the endpoint script in
``endpoint_scripts/large_file.py``:

- chunks: the endpoint gzips the file one chunk at a time and each chunk comes
  back through a regular file retrieval. Needs nothing but XDR, so it works on
  isolated endpoints.
- sftp: the endpoint uploads the file to an SFTP server. Much faster, but the
  endpoint must reach the server.

Both check the SHA-256 of the whole file and both resume after a failure.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import pprint
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import archive
from ..endpoint_scripts import large_file as endpoint_script
from ..errors import XdrError, XdrFileTooLargeError, XdrValidationError
from .base import BaseService

logger = logging.getLogger(__name__)

COPY_BLOCK = 1024 * 1024
# Below this, a chunk refused by the tenant size limit is not split further.
MIN_CHUNK_BYTES = 8 * 1024 * 1024
# Time an action may wait in the queue before the agent picks it up.
QUEUE_GRACE_SECONDS = 600
_CONFIG_BLOCK = re.compile(r"^# XDRLF-CONFIG-BEGIN\n.*?^# XDRLF-CONFIG-END\n", re.DOTALL | re.MULTILINE)

Progress = Callable[[str], None]


def endpoint_script_source() -> str:
    """Return the endpoint script as shipped, with its documented CONFIG."""
    return Path(endpoint_script.__file__).read_text(encoding="utf-8")


def render_script(**config: Any) -> str:
    """Return the endpoint script with CONFIG set to its defaults updated by ``config``."""
    unknown = set(config) - set(endpoint_script.CONFIG)
    if unknown:
        raise XdrValidationError("Unknown endpoint script settings: %s" % ", ".join(sorted(unknown)))
    merged = dict(endpoint_script.CONFIG)
    merged.update(config)
    block = (
        "# XDRLF-CONFIG-BEGIN\n"
        "CONFIG = %s\n"
        "# XDRLF-CONFIG-END\n" % pprint.pformat(merged, width=100, sort_dicts=False)
    )
    rendered, count = _CONFIG_BLOCK.subn(lambda _: block, endpoint_script_source())
    if count != 1:
        raise RuntimeError("CONFIG markers not found in the endpoint script")
    return rendered


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(COPY_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# chunks
# --------------------------------------------------------------------------- #
@dataclass
class ChunkState:
    """Progress of a chunked transfer, saved next to the output to resume it."""

    endpoint_id: str
    path: str
    size: int
    mtime: int
    sha256: Optional[str]
    job_id: str
    next_offset: int = 0
    chunks: List[Dict[str, Any]] = field(default_factory=list)


def state_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".state.json")


def part_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def load_state(destination: Path, endpoint_id: str, path: str) -> Optional[ChunkState]:
    """Return the saved state if it belongs to this transfer and matches the partial file."""
    try:
        state = ChunkState(**json.loads(state_path(destination).read_text()))
    except (OSError, ValueError, TypeError):
        return None
    part = part_path(destination)
    if (
        state.endpoint_id != endpoint_id
        or state.path != path
        or not part.exists()
        or part.stat().st_size < state.next_offset
    ):
        return None
    return state


def save_state(destination: Path, state: ChunkState) -> None:
    tmp = state_path(destination).with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    tmp.replace(state_path(destination))


def append_chunk(gz_path: Path, part: Path, offset: int, expected: Dict[str, Any]) -> None:
    """Decompress ``gz_path`` into ``part`` at ``offset``; check its length and SHA-256."""
    digest = hashlib.sha256()
    length = 0
    with gzip.open(str(gz_path), "rb") as src, part.open("r+b") as dst:
        dst.seek(offset)
        dst.truncate()
        for block in iter(lambda: src.read(COPY_BLOCK), b""):
            digest.update(block)
            dst.write(block)
            length += len(block)
    if length != expected["raw_length"] or digest.hexdigest() != expected["sha256"]:
        raise XdrValidationError(
            "Chunk at offset %d is corrupt: got %d bytes %s, expected %d bytes %s"
            % (offset, length, digest.hexdigest(), expected["raw_length"], expected["sha256"])
        )


# --------------------------------------------------------------------------- #
# sftp
# --------------------------------------------------------------------------- #
@dataclass
class SftpTarget:
    """Where and how to reach the SFTP server.

    ``endpoint_host`` is the server's address as the endpoint reaches it,
    when that differs from ``host`` (e.g. an IP for an endpoint without DNS).
    """

    host: str
    port: int
    username: str
    password: str
    host_key: Optional[str] = None
    endpoint_host: Optional[str] = None


def fingerprint(host_key: str) -> str:
    """OpenSSH SHA256 fingerprint of a "type base64" host key."""
    blob = base64.b64decode(host_key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class SftpSession:
    """Workstation-side SFTP session: pins the host key, resumes, fetches."""

    def __init__(self, target: SftpTarget):
        try:
            import paramiko
        except ImportError:
            raise XdrValidationError("The sftp mode needs paramiko: install xdr-cli[sftp]")
        self.transport = paramiko.Transport((target.host, target.port))
        self.transport.start_client()
        key = self.transport.get_remote_server_key()
        self.host_key = "%s %s" % (key.get_name(), key.get_base64())
        if target.host_key and target.host_key.split()[:2] != self.host_key.split():
            self.transport.close()
            raise XdrValidationError(
                "Host key mismatch for %s: the server sent %s" % (target.host, fingerprint(self.host_key))
            )
        self.transport.auth_password(target.username, target.password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)

    def size(self, path: str) -> Optional[int]:
        try:
            return self.sftp.stat(path).st_size
        except FileNotFoundError:
            return None

    def download(self, path: str, destination: Path) -> str:
        """Download a remote file and return its SHA-256."""
        digest = hashlib.sha256()
        with self.sftp.open(path, "rb") as src, destination.open("wb") as dst:
            src.prefetch()
            for block in iter(lambda: src.read(COPY_BLOCK), b""):
                digest.update(block)
                dst.write(block)
        return digest.hexdigest()

    def remove(self, path: str) -> None:
        self.sftp.remove(path)

    def close(self) -> None:
        self.transport.close()


def fetch_from_sftp(target: SftpTarget, remote_path: str, destination: Path, sha256: Optional[str],
                    delete: bool, progress: Progress) -> None:
    """Download an uploaded file from the SFTP server and check its SHA-256."""
    session = SftpSession(target)
    try:
        progress("downloading sftp://%s/%s to %s" % (target.host, remote_path, destination))
        digest = session.download(remote_path, destination)
        if sha256 and digest != sha256:
            raise XdrValidationError("SHA-256 mismatch: endpoint %s, downloaded %s" % (sha256, digest))
        progress("SHA-256 verified: %s" % digest)
        if delete:
            session.remove(remote_path)
            progress("deleted %s from the SFTP server" % remote_path)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
class LargeFilesService(BaseService):
    """Chunked and SFTP retrieval of files over the get-file limit."""

    poll_interval_seconds = 5.0

    async def resolve_endpoint(self, endpoint_id: Optional[str], hostname: Optional[str]) -> str:
        """Return endpoint_id, or the ID of the one connected endpoint named hostname."""
        if endpoint_id:
            return endpoint_id
        if not hostname:
            raise XdrValidationError("endpoint_id or hostname is required")
        endpoints = await self.client.endpoints.get_by_hostname(hostname)
        connected = [e for e in endpoints if e.get("endpoint_status") == "CONNECTED"]
        if len(connected) != 1:
            raise XdrValidationError(
                "Expected one connected endpoint named %s, found %d" % (hostname, len(connected))
            )
        return connected[0]["endpoint_id"]

    async def run_script(self, endpoint_id: str, code: str, timeout: int) -> Dict[str, Any]:
        """Run an endpoint script action and return the ``data`` of its JSON output."""
        reply = await self.client.scripts.run_snippet_and_wait(
            script_content=code,
            endpoint_ids=[endpoint_id],
            timeout=timeout,
            poll_interval_seconds=self.poll_interval_seconds,
            poll_attempts=int((timeout + QUEUE_GRACE_SECONDS) / max(self.poll_interval_seconds, 1)),
        )
        action_id = reply.get("action_id")
        results = (reply.get("results") or {}).get("results") or [{}]
        stdout = results[0].get("standard_output") or ""
        try:
            output = json.loads(stdout)
        except ValueError:
            raise XdrValidationError(
                "Script action %s ended in %s, output %r %s"
                % (action_id, results[0].get("execution_status"), stdout[:500],
                   (reply.get("results") or {}).get("error_message") or "")
            )
        if output.get("status") != "ok":
            raise XdrValidationError(
                "Script action %s failed: %s" % (action_id, "; ".join(output.get("errors") or []))
            )
        return output["data"]

    async def get_file(self, endpoint_id: str, path: str, destination: Path) -> None:
        """Retrieve one file and write it, extracted, to ``destination``."""
        workdir = Path(tempfile.mkdtemp(prefix=".xdr-cli-", dir=str(destination.parent)))
        try:
            zip_path = workdir / "retrieved_file.zip"
            await self.client.files.retrieve_to_file(
                endpoint_id=endpoint_id,
                paths=[path],
                destination=zip_path,
                poll_interval_seconds=self.poll_interval_seconds,
            )
            extracted = workdir / "files"
            extracted.mkdir()
            archive.extract_retrieval(zip_path, extracted)
            archive.retrieved_file(extracted).replace(destination)
        finally:
            shutil.rmtree(str(workdir), ignore_errors=True)

    async def fetch_chunked(
        self,
        endpoint_id: str,
        path: str,
        destination: Path,
        chunk_bytes: int,
        max_raw_bytes: int,
        verify_hash: bool = True,
        script_timeout: int = 600,
        progress: Progress = logger.info,
    ) -> ChunkState:
        """Copy ``path`` to ``destination``, one file retrieval per gzip chunk.

        Rerunning after a failure resumes from the last good chunk. A chunk
        refused by the tenant size limit is restaged at half the size.
        """
        state = load_state(destination, endpoint_id, path)
        part = part_path(destination)
        if state:
            progress("resuming %s at offset %d" % (path, state.next_offset))
        else:
            progress("stat %s (hash=%s)" % (path, verify_hash))
            info = await self.run_script(
                endpoint_id, render_script(action="stat", path=path, hash=verify_hash), script_timeout
            )
            state = ChunkState(
                endpoint_id=endpoint_id,
                path=path,
                size=info["size"],
                mtime=info["mtime"],
                sha256=info.get("sha256"),
                job_id=uuid.uuid4().hex[:12],
            )
            part.write_bytes(b"")
            save_state(destination, state)
            progress("%s is %d bytes, sha256 %s" % (path, state.size, state.sha256))

        gz_path = destination.with_name(destination.name + ".chunk.gz")
        try:
            while True:
                started = time.monotonic()
                chunk = await self.run_script(
                    endpoint_id,
                    render_script(
                        action="stage",
                        path=path,
                        offset=state.next_offset,
                        job_id=state.job_id,
                        chunk_bytes=chunk_bytes,
                        max_raw_bytes=max_raw_bytes,
                        expect_size=state.size,
                        expect_mtime=state.mtime,
                    ),
                    script_timeout,
                )
                try:
                    await self.get_file(endpoint_id, chunk["chunk_path"], gz_path)
                except XdrFileTooLargeError:
                    if chunk_bytes <= MIN_CHUNK_BYTES:
                        raise
                    chunk_bytes //= 2
                    progress("chunk of %d bytes refused by the tenant size limit, retrying with %d"
                             % (chunk["chunk_size"], chunk_bytes))
                    continue
                append_chunk(gz_path, part, state.next_offset, chunk)
                state.chunks.append({k: chunk[k] for k in ("offset", "raw_length", "chunk_size", "sha256")})
                state.next_offset += chunk["raw_length"]
                save_state(destination, state)
                progress(
                    "chunk %d: %d/%d bytes (%.0f%%), %d compressed, %.0fs"
                    % (len(state.chunks), state.next_offset, state.size,
                       100.0 * state.next_offset / max(state.size, 1), chunk["chunk_size"],
                       time.monotonic() - started)
                )
                if chunk["eof"]:
                    break
        finally:
            if gz_path.exists():
                gz_path.unlink()
            try:
                await self.run_script(endpoint_id, render_script(action="cleanup", job_id=state.job_id),
                                      script_timeout)
            except XdrError as exc:
                progress("could not clean the staging directory on the endpoint: %s" % exc)

        if state.sha256 and sha256_file(part) != state.sha256:
            raise XdrValidationError("SHA-256 mismatch for %s, kept %s and its state" % (destination, part))
        shutil.move(str(part), str(destination))
        state_path(destination).unlink()
        return state

    async def upload_sftp(
        self,
        endpoint_id: str,
        path: str,
        target: SftpTarget,
        remote_path: str,
        time_budget: int = 480,
        compression: bool = False,
        resume: bool = False,
        verify_hash: bool = True,
        progress: Progress = logger.info,
    ) -> Dict[str, Any]:
        """Make the endpoint upload ``path`` to the SFTP server, in as many actions as needed.

        Each action uploads for at most ``time_budget`` seconds, so any size
        fits within the script timeout. Returns the endpoint's stat of the file.
        """
        session = SftpSession(target)
        try:
            if not target.host_key:
                progress("trusting SFTP host key %s on first use" % fingerprint(session.host_key))
            offset = (session.size(remote_path + ".part") or 0) if resume else 0
        finally:
            session.close()

        script_timeout = int(time_budget) + 120
        progress("stat %s (hash=%s)" % (path, verify_hash))
        info = await self.run_script(
            endpoint_id, render_script(action="stat", path=path, hash=verify_hash), script_timeout
        )
        progress("%s is %d bytes, sha256 %s" % (path, info["size"], info.get("sha256")))
        if offset > info["size"]:
            offset = 0
        elif offset:
            progress("resuming upload at offset %d" % offset)

        while True:
            started = time.monotonic()
            result = await self.run_script(
                endpoint_id,
                render_script(
                    action="sftp",
                    path=path,
                    offset=offset,
                    expect_size=info["size"],
                    expect_mtime=info["mtime"],
                    sftp_host=target.endpoint_host or target.host,
                    sftp_port=target.port,
                    sftp_username=target.username,
                    sftp_password=target.password,
                    sftp_host_key=session.host_key,
                    sftp_compression=compression,
                    remote_path=remote_path,
                    time_budget=time_budget,
                    job_id="sftp-%s" % endpoint_id[:8],
                ),
                script_timeout,
            )
            sent = result["next_offset"] - offset
            offset = result["next_offset"]
            progress(
                "uploaded %d/%d bytes (%.0f%%), %.1f MiB/s for this action"
                % (offset, info["size"], 100.0 * offset / max(info["size"], 1),
                   sent / max(time.monotonic() - started, 1) / 2 ** 20)
            )
            if result["eof"]:
                return info
            if sent == 0:
                raise XdrValidationError("No progress at offset %d, is the time budget too short?" % offset)
