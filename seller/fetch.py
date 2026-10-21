"""Read the daily snapshot from a private GitHub repo or a local file."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .core import atomic_json, iso, now
from .ingest import ingest_snapshot
from .schema import MAX_SNAPSHOT_BYTES, SNAPSHOT_POLL_SECONDS, SNAPSHOT_RETRY_SECONDS, SnapshotError

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
USER_AGENT = "AmazonSellerOmarchy/1.2"


def save_github_token(config_dir, token):
    """Store a read-only token next to other credentials, owner-readable only."""
    file = Path(config_dir) / "credentials.json"
    current = {}
    if file.exists():
        mode = file.stat().st_mode
        if mode & 0o077:
            raise SnapshotError("Credentials file must be readable only by your user (mode 0600).")
        loaded = json.loads(file.read_text())
        if isinstance(loaded, dict):
            current = loaded
    if token:
        current["github_token"] = token
    else:
        current.pop("github_token", None)
    if not current and not token:
        if file.exists():
            file.unlink()
        return
    atomic_json(file, current)
    os.chmod(file, 0o600)


def read_github_token(config_dir):
    file = Path(config_dir) / "credentials.json"
    if not file.exists():
        return None
    if file.stat().st_mode & 0o077:
        raise SnapshotError("Credentials file must be readable only by your user (mode 0600).")
    loaded = json.loads(file.read_text())
    if not isinstance(loaded, dict):
        return None
    token = loaded.get("github_token")
    if token in (None, ""):
        return None
    if not isinstance(token, str) or any(char.isspace() for char in token):
        raise SnapshotError("Stored GitHub token is invalid. Run amazon-seller setup again.")
    return token


def gh_token():
    """Return `gh auth token` when the GitHub CLI is installed and logged in."""
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    if not token or any(char.isspace() for char in token):
        return None
    return token


def _github_path(owner, repo, path, branch):
    if not _NAME.fullmatch(owner or "") or not _NAME.fullmatch(repo or ""):
        raise SnapshotError("GitHub owner and repo must be plain names. Run amazon-seller setup.")
    if not _NAME.fullmatch(branch or ""):
        raise SnapshotError("GitHub branch must be a plain name. Run amazon-seller setup.")
    parts = (path or "").split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SnapshotError("Snapshot path must be a file inside the repository.")
    quoted = urllib.parse.quote(path, safe="/")
    return f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/contents/{quoted}?ref={urllib.parse.quote(branch)}"


def github_fetch(owner, repo, path, branch, token):
    if not token:
        raise SnapshotError("No read-only GitHub token. Run amazon-seller setup, or install and log in with gh.")
    url = _github_path(owner, repo, path, branch)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw",
        "Authorization": "Bearer " + token,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise SnapshotError("GitHub rejected the read token. Use a read-only token for the private data repo, or run gh auth login.") from None
        if error.code == 404:
            raise SnapshotError(f"No snapshot at {owner}/{repo} {path} ({branch}).") from None
        raise SnapshotError(f"GitHub request failed (HTTP {error.code}); showing the last saved data") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SnapshotError("GitHub unreachable; showing the last saved data") from None
    if raw[:1] in (b"{", b"["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(payload, dict) and "schema_version" in payload:
            return raw
        if isinstance(payload, dict) and payload.get("encoding") == "base64" and "content" in payload:
            try:
                return base64.b64decode(payload["content"])
            except (ValueError, TypeError):
                raise SnapshotError("GitHub returned a snapshot that could not be read; showing the last saved data") from None
        if isinstance(payload, dict) and "message" in payload and "schema_version" not in payload:
            raise SnapshotError("GitHub could not return the snapshot; showing the last saved data")
    return raw


def load_snapshot_bytes(config, config_dir):
    kind = config.get("source") or "github"
    if kind == "file":
        path = config.get("snapshot_file") or ""
        if not path:
            raise SnapshotError("No snapshot file path. Run amazon-seller setup.")
        file = Path(path).expanduser()
        if not file.is_file():
            raise SnapshotError("Snapshot file not found. Check the path in amazon-seller setup.")
        return file.read_bytes()
    if kind == "spapi":
        raise SnapshotError("Snapshot refresh does not call Amazon. Switch source to github or file.")
    token = read_github_token(config_dir)
    if not token:
        token = gh_token()
    if not token:
        raise SnapshotError("No read-only GitHub token. Run amazon-seller setup, or install and log in with gh.")
    return github_fetch(
        config.get("github_owner"), config.get("github_repo"), config.get("github_path"),
        config.get("github_branch") or "main", token)


def _fail(store, message, at):
    source = store.get("source", "snapshot", {})
    count = int(source.get("failures") or 0) + 1
    delay = min(SNAPSHOT_POLL_SECONDS, SNAPSHOT_RETRY_SECONDS * (2 ** min(count - 1, 4)))
    # Keep the last successful sales/inventory rows. Record only the fetch problem.
    store.source("snapshot", state="error", error=message, failures=count, due=at.timestamp() + delay, fetched_at=iso(at))
    store.db.commit()


def pull_snapshot(store, config, config_dir, force=False):
    """Fetch and ingest when due. Failures leave the previous rows in place."""
    at = now()
    state = store.get("source", "snapshot", {})
    if not force and at.timestamp() < state.get("due", 0):
        return False
    try:
        raw = load_snapshot_bytes(config, config_dir)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("Snapshot is larger than 20 MB; showing the last saved data")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise SnapshotError("Snapshot was not valid JSON; showing the last saved data") from None
        ingest_snapshot(store, data, at)
    except SnapshotError as error:
        _fail(store, str(error), at)
        return False
    except (OSError, ValueError, TypeError, KeyError):
        # SnapshotError is handled above. Unexpected failures must not include file contents.
        _fail(store, "Snapshot could not be applied; showing the last saved data", at)
        return False
    store.source("snapshot", state="ready", error=None, failures=0, due=at.timestamp() + SNAPSHOT_POLL_SECONDS, fetched_at=iso(at))
    store.db.commit()
    return True
