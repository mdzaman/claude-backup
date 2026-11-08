"""Store destination secrets (cloud keys, SAS tokens) outside the settings file.

macOS: the login Keychain, through the ``security`` tool.
Elsewhere (or with CLAUDE_BACKUP_SECRETS=file): ~/.claude-backup/secrets.json,
readable only by you.
"""
import json
import os
import platform
import subprocess
from pathlib import Path

SERVICE = "claude-backup"


class Secrets:
    def __init__(self, base_dir):
        self.file = Path(base_dir) / "secrets.json"
        self.keychain = platform.system() == "Darwin" and os.environ.get("CLAUDE_BACKUP_SECRETS") != "file"

    def set(self, key, value):
        if self.keychain:
            r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", key,
                                "-l", "Claude Backup: " + key, "-w", value], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError("Couldn't save to the Keychain: " + (r.stderr or r.stdout).strip())
            return
        data = self._read()
        data[key] = value
        self._write(data)

    def get(self, key):
        if self.keychain:
            r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", key, "-w"],
                               capture_output=True, text=True)
            return r.stdout.rstrip("\n") if r.returncode == 0 else None
        return self._read().get(key)

    def delete(self, key):
        if self.keychain:
            subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", key], capture_output=True)
            return
        data = self._read()
        if data.pop(key, None) is not None:
            self._write(data)

    def _read(self):
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.file)
