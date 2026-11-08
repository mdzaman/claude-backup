"""App preferences, kept in ~/.claude-backup/settings.json."""
import copy
import json
from pathlib import Path

from .engine import default_output_dir

DEFAULTS = {
    "out_dir": None,               # None = ~/ClaudeBackups
    "keep": 20,                    # backups kept by automatic backups
    "skill_targets": ["claude-code", "upload"],
    "redact": False,
    "accounts": "current",
    "uploaded": {},                # backup path -> names already uploaded (checklist)
    "destinations": [],            # drives and clouds that receive copies of backups
}


class Settings:
    def __init__(self, env):
        self.env = env
        self.path = env.home / ".claude-backup" / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    @property
    def secrets(self):
        from .secrets import Secrets
        return Secrets(self.path.parent)

    def destination(self, dest_id):
        for d in self.data.get("destinations", []):
            if d.get("id") == dest_id or d.get("name") == dest_id:
                return d
        raise KeyError("No destination named %s" % dest_id)

    @property
    def out_dir(self):
        return Path(self.data.get("out_dir") or default_output_dir(self.env))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
