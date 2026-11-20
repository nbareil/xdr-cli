"""Cortex XDR endpoint script: move a file larger than the get-file limit off an endpoint.

XDR snippets take no arguments, so every run is driven by the CONFIG block below.
`xdr-cli get-large-file` rewrites that block for each action. To run the script by
hand from the XDR console (`xdr-cli get-large-file --print-script`), edit CONFIG and
paste the whole file.

Actions:
  stat     size, mtime and optionally the SHA-256 of "path".
  stage    gzip the bytes of "path" starting at "offset" into one chunk file in
           "staging_dir", stopping once the chunk reaches "chunk_bytes" (compressed)
           or "max_raw_bytes" (uncompressed). Older chunks in "staging_dir" are
           deleted first, so at most one chunk sits on disk. Fetch the chunk with
           get-file, then run the next stage.
  cleanup  delete "staging_dir" and the chunks in it.
  sftp     upload "path" to "remote_path" on an SFTP server, starting at "offset",
           for at most "time_budget" seconds. Data goes to "remote_path.part", which
           is renamed to "remote_path" once the whole file is uploaded. Rerun with
           the returned "next_offset" until "eof" is true.

Must stay compatible with Python 3.7 and the XDR module allowlist.
"""

import gzip
import hashlib
import json
import os
import shutil
import socket
import sys
import time

# XDRLF-CONFIG-BEGIN
CONFIG = {
    "action": "stat",
    "path": "C:\\Windows\\MEMORY.DMP",
    "hash": True,
    "offset": 0,
    "staging_dir": None,
    "job_id": "manual",
    "chunk_bytes": 100 * 1024 * 1024,
    "max_raw_bytes": 1024 * 1024 * 1024,
    "expect_size": None,
    "expect_mtime": None,
    "sftp_host": "sftp.example.com",
    "sftp_port": 22,
    "sftp_username": "",
    "sftp_password": "",
    "sftp_host_key": "ssh-ed25519 AAAA...",
    "sftp_compression": False,
    "remote_path": "incoming/file.bin",
    "time_budget": 480,
}
# XDRLF-CONFIG-END

BLOCK = 1024 * 1024
MIN_READ = 64 * 1024
CHUNK_PREFIX = "chunk-"
KNOWN_HOSTS = "known_hosts"
CONNECT_TIMEOUT = 15


class ScriptError(Exception):
    pass


def staging_dir(config):
    if config.get("staging_dir"):
        return config["staging_dir"]
    base = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    return os.path.join(base, "xdrlf-" + str(config["job_id"]))


def file_info(path):
    st = os.stat(path)
    return {"path": path, "size": st.st_size, "mtime": int(st.st_mtime)}


def check_unchanged(config, info):
    # A file that changes between two actions would be reassembled inconsistently.
    if config.get("expect_size") is not None and info["size"] != config["expect_size"]:
        raise ScriptError("size changed: %d -> %d" % (config["expect_size"], info["size"]))
    if config.get("expect_mtime") is not None and info["mtime"] != config["expect_mtime"]:
        raise ScriptError("mtime changed: %d -> %d" % (config["expect_mtime"], info["mtime"]))


def action_stat(config):
    info = file_info(config["path"])
    if config.get("hash"):
        digest = hashlib.sha256()
        with open(config["path"], "rb") as f:
            for block in iter(lambda: f.read(BLOCK), b""):
                digest.update(block)
        info["sha256"] = digest.hexdigest()
    return info


def remove_chunks(directory):
    removed = []
    if not os.path.isdir(directory):
        return removed
    for name in os.listdir(directory):
        if name.startswith(CHUNK_PREFIX):
            os.remove(os.path.join(directory, name))
            removed.append(name)
    return removed


def action_stage(config):
    path = config["path"]
    offset = int(config["offset"])
    chunk_bytes = int(config["chunk_bytes"])
    max_raw = int(config["max_raw_bytes"])
    info = file_info(path)
    check_unchanged(config, info)
    if offset > info["size"]:
        raise ScriptError("offset %d beyond end of file (%d)" % (offset, info["size"]))

    directory = staging_dir(config)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    remove_chunks(directory)
    # Keep a margin so staging never fills the disk.
    free = shutil.disk_usage(directory).free
    if free < 2 * chunk_bytes:
        raise ScriptError("only %d bytes free in %s" % (free, directory))

    chunk_path = os.path.join(directory, "%s%016d.gz" % (CHUNK_PREFIX, offset))
    digest = hashlib.sha256()
    raw = 0
    try:
        with open(path, "rb") as src, open(chunk_path, "wb") as out:
            src.seek(offset)
            with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=6, mtime=0) as gz:
                # out.tell() lags behind by the compressor buffer, so the chunk can
                # overshoot chunk_bytes by a few hundred KiB.
                while raw < max_raw and out.tell() < chunk_bytes:
                    want = min(BLOCK, max_raw - raw, max(chunk_bytes - out.tell(), MIN_READ))
                    block = src.read(want)
                    if not block:
                        break
                    digest.update(block)
                    gz.write(block)
                    raw += len(block)
    except Exception:
        try:
            os.remove(chunk_path)
        except OSError:
            pass
        raise

    info.update(
        {
            "offset": offset,
            "raw_length": raw,
            "eof": offset + raw >= info["size"],
            "sha256": digest.hexdigest(),
            "chunk_path": chunk_path,
            "chunk_size": os.path.getsize(chunk_path),
        }
    )
    return info


def action_cleanup(config):
    directory = staging_dir(config)
    removed = remove_chunks(directory)
    if os.path.isdir(directory):
        os.rmdir(directory)
    return {"staging_dir": directory, "removed": removed}


def check_reachable(host, port):
    # paramiko connects without a timeout, fail fast on firewalled or isolated hosts.
    try:
        socket.create_connection((host, port), timeout=CONNECT_TIMEOUT).close()
    except OSError as exc:
        raise ScriptError("cannot reach %s:%d from the endpoint: %s" % (host, port, exc)) from exc


def sftp_connect(config, directory):
    import pysftp

    check_reachable(config["sftp_host"], int(config["sftp_port"]))

    # pysftp only verifies host keys against a known_hosts file, and the XDR
    # allowlist has no tempfile, so write one into our own staging directory.
    if not os.path.isdir(directory):
        os.makedirs(directory)
    known_hosts = os.path.join(directory, KNOWN_HOSTS)
    with open(known_hosts, "w") as f:
        f.write("%s %s\n" % (config["sftp_host"], config["sftp_host_key"]))
    try:
        cnopts = pysftp.CnOpts(knownhosts=known_hosts)
    finally:
        os.remove(known_hosts)
        try:
            os.rmdir(directory)
        except OSError:
            pass
    cnopts.compression = bool(config.get("sftp_compression"))
    return pysftp.Connection(
        config["sftp_host"],
        username=config["sftp_username"],
        password=config["sftp_password"],
        port=int(config["sftp_port"]),
        cnopts=cnopts,
    )


def action_sftp(config):
    path = config["path"]
    offset = int(config["offset"])
    info = file_info(path)
    check_unchanged(config, info)
    if offset > info["size"]:
        raise ScriptError("offset %d beyond end of file (%d)" % (offset, info["size"]))

    remote = config["remote_path"]
    part = remote + ".part"
    directory = staging_dir(config)
    conn = sftp_connect(config, directory)
    try:
        client = conn.sftp_client
        if offset == 0:
            mode = "wb"
        else:
            remote_size = client.stat(part).st_size
            if remote_size < offset:
                raise ScriptError(
                    "%s holds %d bytes, cannot resume at %d" % (part, remote_size, offset)
                )
            mode = "r+b"
        position = offset
        with open(path, "rb") as src, client.open(part, mode) as dst:
            dst.set_pipelined(True)
            if offset:
                dst.truncate(offset)
                dst.seek(offset)
            src.seek(offset)
            # Count the budget from here so slow connections still upload at least one block.
            deadline = time.time() + float(config["time_budget"])
            while True:
                block = src.read(BLOCK)
                if not block:
                    break
                dst.write(block)
                position += len(block)
                if time.time() >= deadline:
                    break
        eof = position >= info["size"]
        remote_size = client.stat(part).st_size
        if remote_size != position:
            raise ScriptError("%s holds %d bytes, expected %d" % (part, remote_size, position))
        if eof:
            # Some servers refuse the SFTPv3 rename but accept the OpenSSH
            # posix-rename extension, which also overwrites an existing file.
            try:
                client.posix_rename(part, remote)
            except IOError:
                try:
                    client.remove(remote)
                except IOError:
                    pass
                client.rename(part, remote)
    finally:
        conn.close()

    info.update(
        {
            "offset": offset,
            "next_offset": position,
            "eof": eof,
            "remote_path": remote if eof else part,
        }
    )
    return info


ACTIONS = {
    "stat": action_stat,
    "stage": action_stage,
    "cleanup": action_cleanup,
    "sftp": action_sftp,
}


def main(config):
    action = config.get("action")
    result = {"status": "ok", "action": action, "data": {}, "errors": []}
    try:
        if action not in ACTIONS:
            raise ScriptError("unknown action %r" % action)
        result["data"] = ACTIONS[action](config)
    except Exception as exc:
        result["status"] = "error"
        result["errors"].append("%s: %s" % (type(exc).__name__, exc))
    return result


if __name__ == "__main__":
    output = main(CONFIG)
    sys.stdout.write(json.dumps(output, separators=(",", ":"), sort_keys=True) + "\n")
