"""Locate Claude Desktop / Claude Code data on this machine and the accounts it holds."""
import json
import os
import platform
import re
import subprocess
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def default_desktop_dir(home):
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Claude" if appdata else home / "AppData" / "Roaming" / "Claude"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else home / ".config") / "Claude"


class Env:
    """Physical locations of the logical roots that a backup refers to.

    Archives never store absolute paths. Every file is recorded against one of
    these roots, so a backup taken on one machine restores on another.
    """

    def __init__(self, home=None, desktop=None, claude_home=None):
        self.home = Path(home or os.environ.get("CLAUDE_BACKUP_HOME") or Path.home())
        self.claude_home = Path(
            claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or self.home / ".claude"
        )
        self.desktop = Path(desktop or default_desktop_dir(self.home))
        self.roots = {
            "CLAUDE_HOME": self.claude_home,
            "HOME": self.home,
            "DESKTOP": self.desktop,
        }
        cowork = self.cowork_files_dir()
        if cowork:
            self.roots["COWORK_FILES"] = cowork

    # ----- files -----------------------------------------------------------
    @property
    def claude_json(self):
        # Claude Code keeps ~/.claude.json next to (not inside) ~/.claude,
        # unless CLAUDE_CONFIG_DIR relocates both.
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            return self.claude_home / ".claude.json"
        return self.home / ".claude.json"

    @property
    def desktop_config(self):
        return self.desktop / "claude_desktop_config.json"

    @property
    def agent_sessions(self):
        return self.desktop / "local-agent-mode-sessions"

    def skills_dir(self, account, org):
        return self.agent_sessions / "skills-plugin" / org / account

    def account_dir(self, account, org):
        return self.agent_sessions / account / org

    def cowork_files_dir(self):
        cfg = _read_json(self.desktop / "claude_desktop_config.json") or {}
        p = cfg.get("coworkUserFilesPath")
        if p and Path(p).is_dir():
            return Path(p)
        return None

    def resolve(self, root, rel=""):
        base = self.roots.get(root)
        if base is None:
            raise KeyError("Root %s is not available on this machine" % root)
        return base / rel if rel else base

    # ----- accounts --------------------------------------------------------
    def current_account(self):
        """(account_uuid, org_uuid, email) of the account signed in now, if known."""
        cj = _read_json(self.claude_json) or {}
        oa = cj.get("oauthAccount") or {}
        if oa.get("accountUuid") and oa.get("organizationUuid"):
            return oa["accountUuid"], oa["organizationUuid"], oa.get("emailAddress")
        # Fall back to the desktop app's record of the last signed-in account.
        # Only these two keys are read; config.json also holds OAuth tokens
        # and is never copied into a backup.
        cfg = _read_json(self.desktop / "config.json") or {}
        acct = cfg.get("lastKnownAccountUuid")
        if not acct:
            return None, None, None
        org = (cfg.get("sshTransportFlagLastAnswer") or {}).get("orgUuid")
        if not org:
            orgs = [a["org"] for a in self.accounts() if a["account"] == acct]
            org = orgs[0] if len(orgs) == 1 else None
        return acct, org, None

    def accounts(self):
        """Every (account, org) pair that has local data on this machine."""
        pairs = {}
        if self.agent_sessions.is_dir():
            for a in self.agent_sessions.iterdir():
                if a.is_dir() and UUID_RE.match(a.name):
                    for o in a.iterdir():
                        if o.is_dir() and UUID_RE.match(o.name):
                            pairs.setdefault((a.name, o.name), {})
            sp = self.agent_sessions / "skills-plugin"
            if sp.is_dir():
                for o in sp.iterdir():
                    if o.is_dir() and UUID_RE.match(o.name):
                        for a in o.iterdir():
                            if a.is_dir() and UUID_RE.match(a.name):
                                pairs.setdefault((a.name, o.name), {})
        cj = _read_json(self.claude_json) or {}
        oa = cj.get("oauthAccount") or {}
        if oa.get("accountUuid") and oa.get("organizationUuid"):
            pairs.setdefault((oa["accountUuid"], oa["organizationUuid"]), {})
        cur = None
        if oa.get("accountUuid"):
            cur = (oa.get("accountUuid"), oa.get("organizationUuid"))
        out = []
        for (acct, org) in sorted(pairs):
            user_skills = len(self.user_skill_entries(acct, org))
            info = {
                "account": acct,
                "org": org,
                "current": cur == (acct, org),
                "user_skills": user_skills,
                "email": oa.get("emailAddress") if cur == (acct, org) else None,
                "org_name": oa.get("organizationName") if cur == (acct, org) else None,
            }
            out.append(info)
        return out

    def user_skill_entries(self, account, org):
        """Manifest entries for skills the user created (not Anthropic-managed)."""
        manifest = _read_json(self.skills_dir(account, org) / "manifest.json") or {}
        return [s for s in manifest.get("skills", []) if s.get("creatorType") == "user"]

    # ----- process ---------------------------------------------------------
    def desktop_running(self):
        system = platform.system()
        try:
            if system == "Darwin":
                r = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True)
                return any(line.strip().endswith("Claude.app/Contents/MacOS/Claude")
                           for line in r.stdout.splitlines())
            if system == "Windows":
                r = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq Claude.exe"], capture_output=True, text=True
                )
                return "Claude.exe" in r.stdout
            r = subprocess.run(["pgrep", "-x", "claude-desktop"], capture_output=True)
            return r.returncode == 0
        except OSError:
            return False
