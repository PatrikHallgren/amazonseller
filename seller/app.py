"""CLI entry points and a single background publisher for the Omarchy panel."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from .api import APIError, Amazon
from .core import Store, atomic_json, detect_alerts, dt, iso, load_config, now, paths, snapshot, validate_settings
from .demo import seed
from .sync import Sync


def open_store(data_dir, config):
    return Store(data_dir / ("demo.sqlite3" if config["mode"] == "demo" else "seller.sqlite3"))


def publish(store, config, data_dir):
    at = now()
    store.db.execute("BEGIN IMMEDIATE")
    if config["mode"] == "demo":
        seed(store, at)
    view = snapshot(store, config, at)
    detect_alerts(store, view, at)
    view = snapshot(store, config, at)
    store.db.commit()
    atomic_json(data_dir / "snapshot.json", view)
    return view


def change_mode(config_dir, config, mode):
    """Keep demo product selections from hiding real products (and vice versa)."""
    if mode == config["mode"]:
        return config
    keys = ("selected", "pinned", "overrides")
    atomic_json(config_dir / (config["mode"]+"-products.json"), {k:config[k] for k in keys})
    file = config_dir / (mode+"-products.json")
    profile = json.loads(file.read_text()) if file.exists() else {"selected":None, "pinned":[], "overrides":{}}
    return config | profile | {"mode":mode}


def notifications(store, view, resume=False):
    if view["mode"] == "demo" or not view["settings"]["notifications"]:
        return
    at = now()
    # Send normal desktop notifications; the Omarchy daemon owns Do Not Disturb.
    candidates = []
    for a in view["alerts"]:
        row = store.db.execute("SELECT * FROM alerts WHERE id=?", (a["id"],)).fetchone()
        if row["acknowledged"] or (row["snoozed_until"] and dt(row["snoozed_until"]) > at):
            continue
        if not row["notified"] or resume or row["snoozed_until"]:
            candidates.append(a)
    if not candidates:
        return
    body = "\n".join(a["title"] for a in candidates[:5])
    if len(candidates) > 5:
        body += f"\n…and {len(candidates)-5} more. Open Amazon Seller in the bar."
    try:
        subprocess.run(["notify-send", "-a", "Amazon Seller", "-u", "normal", "-i", "dialog-warning",
                        f"Amazon Seller · {len(candidates)} need attention", body], check=True, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return
    for a in candidates:
        store.db.execute("UPDATE alerts SET notified=1,snoozed_until=NULL WHERE id=?", (a["id"],))
    store.db.commit()


def daemon(config_dir, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(data_dir / "service.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last = time.time()
    sync = None
    store = None
    identity = None
    while not stopped:
        try:
            config = load_config(config_dir)
            current_identity = config["mode"]
            if current_identity != identity:
                if store:
                    store.db.close()
                store = open_store(data_dir, config)
                sync = None
                identity = current_identity
            resume = time.time()-last > 90
            last = time.time()
            request = data_dir / "refresh.request"
            force = request.exists() or resume
            if request.exists():
                request.unlink()
            if config["mode"] == "live":
                credentials_file = config_dir / "credentials.json"
                if credentials_file.exists():
                    if credentials_file.stat().st_mode & 0o077:
                        raise ValueError("Credentials file must have mode 0600")
                    if sync is None or force:
                        sync = Sync(store, json.loads(credentials_file.read_text()))
                    sync.tick(force)
                else:
                    store.source("orders", state="setup", error="Connect your private Amazon application to start syncing")
                    store.db.commit()
            view = publish(store, config, data_dir)
            notifications(store, view, resume)
        except APIError as error:
            if store:
                store.db.rollback()
                store.source("orders", state="error", error=str(error))
                store.db.commit()
                publish(store, config, data_dir)
            print(str(error), file=sys.stderr, flush=True)
        except Exception as error:
            # Exception type is diagnostic; raw values from remote responses are not logged.
            print("Sync error: " + type(error).__name__, file=sys.stderr, flush=True)
        for _ in range(10):
            if stopped:
                break
            time.sleep(1)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="amazon-seller", description="US FBA desktop dashboard")
    parser.add_argument("--root", help="Isolated configuration/data root for testing")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("daemon", "status", "snapshot", "refresh", "setup", "verify"):
        sub.add_parser(command)
    sub.add_parser("settings").add_argument("json", nargs="?", help="JSON settings patch; omit to read")
    sub.add_parser("mode").add_argument("value", choices=["demo", "live"])
    alert = sub.add_parser("alert")
    alert.add_argument("action", choices=["ack", "snooze"])
    alert.add_argument("id")
    args = parser.parse_args(argv)
    config_dir, data_dir = paths(args.root)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = load_config(config_dir)
    if args.command == "daemon":
        return daemon(config_dir, data_dir)
    if args.command == "setup":
        if not sys.stdin.isatty():
            raise ValueError("Run setup in a terminal; do not pass credentials as command arguments")
        print("Create a private SP-API application first. See README.md for roles and authorization.\nCredentials are saved locally with owner-only permissions.")
        values = {"seller_id": input("Seller ID (merchant token): ").strip(), "client_id": input("LWA client ID: ").strip(),
                  "client_secret": getpass.getpass("LWA client secret: ").strip(), "refresh_token": getpass.getpass("LWA refresh token: ").strip()}
        if not all(values.values()):
            raise ValueError("All four fields are required")
        api = Amazon(values)
        api.verify()
        atomic_json(config_dir / "credentials.json", values)
        store = Store(data_dir / "seller.sqlite3")
        store.put("meta", "verified", {"marketplace": "ATVPDKIKX0DER", "at": iso(now())})
        store.db.commit()
        config = change_mode(config_dir, config, "live")
        atomic_json(config_dir / "settings.json", config)
        atomic_json(data_dir / "refresh.request", {})
        print("US account verified. Live synchronization will start shortly.")
        return
    if args.command == "verify":
        file = config_dir / "credentials.json"
        if not file.exists():
            raise ValueError("Run amazon-seller setup first")
        Amazon(json.loads(file.read_text())).verify()
        print("US marketplace and seller ID verified")
        return
    if args.command in ("settings", "mode"):
        if args.command == "settings" and args.json is None:
            print(json.dumps(config))
            return
        changes = json.loads(args.json) if args.command == "settings" else {"mode": args.value}
        if not isinstance(changes, dict):
            raise ValueError("Settings patch must be a JSON object")
        validate_settings(changes)
        # Serialize concurrent settings changes without losing unrelated keys.
        with open(config_dir / "settings.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = load_config(config_dir)
            if "mode" in changes:
                config = change_mode(config_dir, config, changes["mode"])
            config = config | changes
            atomic_json(config_dir / "settings.json", config)
    store = open_store(data_dir, config)
    if args.command == "alert":
        if args.action == "ack":
            store.db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (args.id,))
        else:
            store.db.execute("UPDATE alerts SET snoozed_until=? WHERE id=?", (iso(now()+timedelta(hours=24)), args.id))
        store.db.commit()
    if args.command == "refresh":
        atomic_json(data_dir / "refresh.request", {})
        print(json.dumps({"queued": True}))
        return
    view = publish(store, config, data_dir)
    if args.command == "status":
        view = {k: view[k] for k in ("mode", "setup_required", "stale", "selected_count", "sources", "history")}
    print(json.dumps(view, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, APIError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
