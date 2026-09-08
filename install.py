#!/usr/bin/env python3
"""Install/remove only this plugin's files; no package-managed Omarchy changes."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime

PLUGIN = "patrik.amazon-seller"
SOURCE = Path(__file__).resolve().parent
HOME_DIR = Path.home()
TARGET = HOME_DIR / ".config/omarchy/plugins" / PLUGIN
CLI = HOME_DIR / ".local/bin/amazon-seller"
UNIT = HOME_DIR / ".config/systemd/user/amazon-seller.service"


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "remove", "check"))
    parser.add_argument("--demo", action="store_true", help="Start in clearly labeled demo mode")
    args = parser.parse_args()
    if args.action == "check":
        print(json.dumps({"source": str(SOURCE), "plugin": str(TARGET), "cli": str(CLI), "service": str(UNIT), "existing_plugin": TARGET.exists()}, indent=2))
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if TARGET.exists():
        manifest = TARGET / "manifest.json"
        if not manifest.exists() or json.loads(manifest.read_text()).get("id") != PLUGIN:
            raise SystemExit("Refusing to replace an unrelated plugin directory")
    if args.action == "remove":
        if TARGET.exists():
            run("omarchy", "plugin", "disable", PLUGIN)
        subprocess.run(["systemctl", "--user", "disable", "--now", "amazon-seller.service"], check=False)
        backup = HOME_DIR / ".local/share/amazon-seller/backups" / stamp
        backup.mkdir(parents=True, exist_ok=True)
        if TARGET.exists():
            shutil.move(str(TARGET), str(backup / "plugin"))
        if UNIT.exists():
            shutil.move(str(UNIT), str(backup / "amazon-seller.service"))
        if CLI.is_symlink() and CLI.readlink() == TARGET / "amazon-seller":
            CLI.unlink()
        run("systemctl", "--user", "daemon-reload")
        print("Plugin removed. Configuration and business data retained; recoverable files: " + str(backup))
        return
    if CLI.exists() and (not CLI.is_symlink() or CLI.resolve() != TARGET / "amazon-seller"):
        raise SystemExit("Refusing to overwrite an unrelated amazon-seller executable")
    if TARGET.exists() and TARGET.resolve() != SOURCE:
        backup = HOME_DIR / ".local/share/amazon-seller/backups" / stamp / "plugin"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(TARGET, backup, ignore=shutil.ignore_patterns("__pycache__"))
    if TARGET.resolve() != SOURCE:
        shutil.copytree(SOURCE, TARGET, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests", "artifacts"))
    os.chmod(TARGET / "amazon-seller", 0o755)
    CLI.parent.mkdir(parents=True, exist_ok=True)
    if not CLI.is_symlink():
        CLI.symlink_to(TARGET / "amazon-seller")
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    unit = f'''[Unit]
Description=Amazon Seller local dashboard sync
After=graphical-session.target network.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 "{TARGET / 'amazon-seller'}" daemon
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=graphical-session.target
'''
    UNIT.write_text(unit)
    run("systemctl", "--user", "daemon-reload")
    if args.demo:
        subprocess.run(["python3", str(CLI), "mode", "demo"], check=True, stdout=subprocess.DEVNULL)
    else:
        subprocess.run(["python3", str(CLI), "snapshot"], check=True, stdout=subprocess.DEVNULL)
    run("systemctl", "--user", "enable", "--now", "amazon-seller.service")
    run("systemctl", "--user", "restart", "amazon-seller.service")
    run("omarchy-shell", "shell", "rescanPlugins")
    run("omarchy", "plugin", "enable", PLUGIN)
    print("Installed Amazon Seller. Click the bag icon in your bar.")


if __name__ == "__main__":
    main()
