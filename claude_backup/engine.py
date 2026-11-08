"""Backup, restore and account migration for Claude Desktop and Claude Code.

A backup is a zip file (``*.claudebackup``) holding ``manifest.json`` and a
``data/`` tree. Each item in the manifest names a logical root (CLAUDE_HOME,
DESKTOP, ...) and a path template in which the account and organization IDs
are written as ``{account}`` and ``{org}``. Restoring fills those in with the
target account, which is what lets a backup move between accounts.
"""
import copy
import datetime as _dt
import hashlib
import io
import json
import os
import platform
import re
import shutil
import zipfile
from pathlib import Path

from . import __version__
from .claude_ai_export import read_export
from .env import Env

FORMAT = "claude-backup"
FORMAT_VERSION = 1
EXT = ".claudebackup"

CATEGORIES = {
    "skills": "Skills you created in Claude Desktop / claude.ai, and Claude Code skills",
    "plugins": "Plugins you uploaded or installed",
    "prompts": "CLAUDE.md, slash commands, subagents, output styles, memory, project instructions",
    "artifacts": "Artifacts (from a claude.ai export) and Cowork output files",
    "settings": "MCP servers, app preferences, Claude Code settings, scheduled tasks",
    "history": "Claude Code transcripts, session metadata, claude.ai conversations",
}

SKILL_TARGETS = {
    "claude-code": "Install into ~/.claude/skills (works in Claude Code for any account)",
    "upload": "Write .zip files to upload in Claude > Settings > Capabilities > Skills",
    "desktop": "Write into the Claude Desktop local skills cache for the target account",
}

# Never copied: credentials, device keys, lock files.
SKIP_NAMES = {".DS_Store", ".credentials.json", "buddy-tokens.json", "link-key",
              "__pycache__", "node_modules", "Thumbs.db"}
SKIP_SUFFIXES = (".lock", ".pyc")
SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd|credential|cookie|authorization)", re.I)
SECRET_VALUE_RE = re.compile(r"^(sk-[A-Za-z0-9_-]{10,}|ghp_|gho_|github_pat_|xox[abpr]-|AKIA[0-9A-Z]{12}|AIza)")
REDACTED = "<REDACTED>"
# Machine-specific keys stripped from the desktop config.
DESKTOP_CONFIG_DROP = {"coworkUserFilesPath"}
CLAUDE_JSON_KEEP = {"mcpServers"}
CLAUDE_JSON_PROJECT_KEEP = {"mcpServers", "allowedTools", "enabledMcpjsonServers",
                            "disabledMcpjsonServers", "mcpContextUris"}


class BackupError(Exception):
    pass


class Cancelled(BackupError):
    """Raised from a progress callback to stop a backup or restore."""


def default_output_dir(env=None):
    env = env or Env()
    return env.home / "ClaudeBackups"


def now_stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------
class Item:
    """One restorable unit: a skill, a settings file, a project's transcripts..."""

    def __init__(self, id, category, title, root=None, rel=None, restore="copy", meta=None):
        self.id = id
        self.category = category
        self.title = title
        self.root = root          # logical root, or None for export-only items
        self.rel = rel            # path template under root ({account}/{org} allowed)
        self.restore = restore    # copy | json-merge | claude-json | skill | plugin | export
        self.meta = meta or {}
        self.entries = []         # (relative path, Path | bytes)

    def add_file(self, relpath, source):
        self.entries.append((relpath.replace(os.sep, "/"), source))

    def add_tree(self, directory, skip_dirs=()):
        for rel, path in walk(directory, skip_dirs):
            self.add_file(rel, path)

    def size(self):
        total = 0
        for _, src in self.entries:
            if isinstance(src, bytes):
                total += len(src)
            else:
                try:
                    total += src.stat().st_size
                except OSError:
                    pass
        return total

    @property
    def safe_id(self):
        return re.sub(r"[^\w.@-]+", "__", self.id)


def walk(directory, skip_dirs=()):
    directory = Path(directory)
    if not directory.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_NAMES and d not in skip_dirs
                             and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if fn in SKIP_NAMES or fn.endswith(SKIP_SUFFIXES) or full.is_symlink():
                continue
            if fn.startswith(".claude.json.backup."):
                continue
            yield str(full.relative_to(directory)), full


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _json_bytes(obj):
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def redact(obj):
    """Blank out values that look like API keys or tokens."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v and (SECRET_KEY_RE.search(k) or SECRET_VALUE_RE.match(v)):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [REDACTED if isinstance(v, str) and SECRET_VALUE_RE.match(v) else redact(v) for v in obj]
    return obj


def _tmpl(path_str, account, org):
    """Replace concrete account/org IDs in a relative path with placeholders."""
    if account:
        path_str = path_str.replace(account, "{account}")
    if org:
        path_str = path_str.replace(org, "{org}")
    return path_str


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------
def resolve_accounts(env, accounts):
    """accounts: 'current' | 'all' | [(account, org), ...] -> list of pairs."""
    if accounts in (None, "current"):
        acct, org, _ = env.current_account()
        return [(acct, org)] if acct and org else []
    if accounts == "all":
        return [(a["account"], a["org"]) for a in env.accounts()]
    return [tuple(p) for p in accounts]


def collect(env, categories, accounts="current", export_zip=None, redact_secrets=False):
    categories = set(categories or CATEGORIES)
    pairs = resolve_accounts(env, accounts)
    multi = len(pairs) > 1
    items = []
    ch = env.claude_home

    def acct_suffix(acct):
        return "@" + acct[:8] if multi else ""

    # ---- skills ---------------------------------------------------------
    if "skills" in categories:
        for acct, org in pairs:
            sdir = env.skills_dir(acct, org)
            for entry in env.user_skill_entries(acct, org):
                name = entry.get("name")
                src = sdir / "skills" / name
                if not name or not src.is_dir():
                    continue
                it = Item("skill:%s%s" % (name, acct_suffix(acct)), "skills", name,
                          "DESKTOP",
                          "local-agent-mode-sessions/skills-plugin/{org}/{account}/skills/" + name,
                          "skill", {"source": "desktop", "account": acct, "org": org,
                                    "name": name, "manifest_entry": entry,
                                    "description": entry.get("description", "")})
                it.add_tree(src)
                items.append(it)
        cc_skills = ch / "skills"
        if cc_skills.is_dir():
            for d in sorted(cc_skills.iterdir()):
                if d.is_dir() and (d / "SKILL.md").exists():
                    it = Item("skill-cc:" + d.name, "skills", d.name, "CLAUDE_HOME",
                              "skills/" + d.name, "skill",
                              {"source": "claude-code", "name": d.name,
                               "description": _skill_description(d / "SKILL.md")})
                    it.add_tree(d)
                    items.append(it)

    # ---- plugins --------------------------------------------------------
    if "plugins" in categories:
        for acct, org in pairs:
            rpm = env.account_dir(acct, org) / "rpm"
            manifest = _read_json(rpm / "manifest.json") or {}
            for p in manifest.get("plugins", []):
                if p.get("installedBy") != "user" or not (rpm / p.get("id", "")).is_dir():
                    continue
                it = Item("plugin:%s%s" % (p.get("name"), acct_suffix(acct)), "plugins",
                          p.get("displayName") or p.get("name"), "DESKTOP",
                          "local-agent-mode-sessions/{account}/{org}/rpm/" + p["id"], "plugin",
                          {"account": acct, "org": org, "name": p.get("name"),
                           "manifest_entry": p, "marketplace": p.get("marketplaceName")})
                it.add_tree(rpm / p["id"])
                items.append(it)
            km = env.account_dir(acct, org) / "cowork_plugins" / "known_marketplaces.json"
            if km.exists():
                it = Item("plugins:marketplaces%s" % acct_suffix(acct), "plugins",
                          "Cowork plugin marketplaces", "DESKTOP",
                          "local-agent-mode-sessions/{account}/{org}/cowork_plugins/known_marketplaces.json",
                          "json-merge", {"account": acct, "org": org, "file": True})
                it.add_file("known_marketplaces.json", km)
                items.append(it)
        cc_plugins = ch / "plugins"
        if cc_plugins.is_dir():
            it = Item("plugins:claude-code", "plugins", "Claude Code plugins & marketplaces",
                      "CLAUDE_HOME", "plugins", "copy")
            it.add_tree(cc_plugins, skip_dirs=("cache",))
            if it.entries:
                items.append(it)

    # ---- prompts --------------------------------------------------------
    if "prompts" in categories:
        if (ch / "CLAUDE.md").is_file():
            it = Item("prompts:CLAUDE.md", "prompts", "Global instructions (CLAUDE.md)",
                      "CLAUDE_HOME", "CLAUDE.md", "copy", {"file": True})
            it.add_file("CLAUDE.md", ch / "CLAUDE.md")
            items.append(it)
        for sub, title in (("commands", "Slash commands"), ("agents", "Subagents"),
                           ("output-styles", "Output styles")):
            if (ch / sub).is_dir():
                it = Item("prompts:" + sub, "prompts", title, "CLAUDE_HOME", sub, "copy")
                it.add_tree(ch / sub)
                if it.entries:
                    items.append(it)
        projects = ch / "projects"
        if projects.is_dir():
            for proj in sorted(projects.iterdir()):
                if (proj / "memory").is_dir():
                    it = Item("prompts:memory:" + proj.name, "prompts",
                              "Memory: " + _project_label(proj.name), "CLAUDE_HOME",
                              "projects/%s/memory" % proj.name, "copy")
                    it.add_tree(proj / "memory")
                    if it.entries:
                        items.append(it)

    # ---- artifacts ------------------------------------------------------
    if "artifacts" in categories:
        cowork = env.roots.get("COWORK_FILES")
        if cowork and cowork.is_dir():
            it = Item("artifacts:cowork-files", "artifacts", "Cowork files (%s)" % cowork.name,
                      "COWORK_FILES", "", "copy")
            it.add_tree(cowork, skip_dirs=(".git",))
            if it.entries:
                items.append(it)

    # ---- settings -------------------------------------------------------
    if "settings" in categories:
        cfg = _read_json(env.desktop_config)
        if cfg is not None:
            cfg = {k: v for k, v in cfg.items() if k not in DESKTOP_CONFIG_DROP}
            if redact_secrets:
                cfg = redact(cfg)
            it = Item("settings:desktop-config", "settings",
                      "Claude Desktop config (MCP servers, preferences)", "DESKTOP",
                      "claude_desktop_config.json", "json-merge", {"file": True})
            it.add_file("claude_desktop_config.json", _json_bytes(cfg))
            items.append(it)
        for fname, title in (("settings.json", "Claude Code settings"),
                             ("keybindings.json", "Claude Code keybindings")):
            data = _read_json(ch / fname)
            if data is not None:
                it = Item("settings:" + fname, "settings", title, "CLAUDE_HOME", fname,
                          "json-merge", {"file": True})
                it.add_file(fname, _json_bytes(redact(data) if redact_secrets else data))
                items.append(it)
        cj = _read_json(env.claude_json)
        if cj:
            keep = {k: cj[k] for k in CLAUDE_JSON_KEEP if k in cj}
            projs = {}
            for path, p in (cj.get("projects") or {}).items():
                sub = {k: p[k] for k in CLAUDE_JSON_PROJECT_KEEP if p.get(k)}
                if sub:
                    projs[path] = sub
            if projs:
                keep["projects"] = projs
            if keep:
                if redact_secrets:
                    keep = redact(keep)
                it = Item("settings:claude-json", "settings",
                          "Claude Code MCP servers & per-project permissions", "HOME",
                          os.path.relpath(env.claude_json, env.home), "claude-json", {"file": True})
                it.add_file(".claude.json", _json_bytes(keep))
                items.append(it)
        for acct, org in pairs:
            adir = env.account_dir(acct, org)
            for fname in ("cowork_settings.json", "scheduled-tasks.json"):
                if (adir / fname).is_file():
                    it = Item("settings:%s%s" % (fname, acct_suffix(acct)), "settings",
                              "Cowork " + fname.replace(".json", "").replace("_", " ").replace("-", " "),
                              "DESKTOP", "local-agent-mode-sessions/{account}/{org}/" + fname,
                              "copy", {"account": acct, "org": org, "file": True})
                    it.add_file(fname, adir / fname)
                    items.append(it)
            st = env.desktop / "claude-code-sessions" / acct / org / "scheduled-tasks.json"
            if st.is_file():
                it = Item("settings:code-scheduled-tasks%s" % acct_suffix(acct), "settings",
                          "Claude Code (desktop) scheduled tasks", "DESKTOP",
                          "claude-code-sessions/{account}/{org}/scheduled-tasks.json", "copy",
                          {"account": acct, "org": org, "file": True})
                it.add_file("scheduled-tasks.json", st)
                items.append(it)

    # ---- history --------------------------------------------------------
    if "history" in categories:
        projects = ch / "projects"
        if projects.is_dir():
            for proj in sorted(projects.iterdir()):
                if proj.is_dir():
                    it = Item("history:project:" + proj.name, "history",
                              "Transcripts: " + _project_label(proj.name), "CLAUDE_HOME",
                              "projects/" + proj.name, "copy")
                    it.add_tree(proj, skip_dirs=("memory",))
                    if it.entries:
                        items.append(it)
        for name in ("history.jsonl",):
            if (ch / name).is_file():
                it = Item("history:" + name, "history", "Prompt history (" + name + ")",
                          "CLAUDE_HOME", name, "copy", {"file": True})
                it.add_file(name, ch / name)
                items.append(it)
        for sub in ("todos", "plans"):
            if (ch / sub).is_dir():
                it = Item("history:" + sub, "history", "Claude Code " + sub, "CLAUDE_HOME", sub, "copy")
                it.add_tree(ch / sub)
                if it.entries:
                    items.append(it)
        for acct, org in pairs:
            d = env.desktop / "claude-code-sessions" / acct / org
            if d.is_dir():
                it = Item("history:desktop-code-sessions%s" % acct_suffix(acct), "history",
                          "Claude Code (desktop) session metadata", "DESKTOP",
                          "claude-code-sessions/{account}/{org}", "copy",
                          {"account": acct, "org": org})
                for rel, path in walk(d):
                    if rel != "scheduled-tasks.json":
                        it.add_file(rel, path)
                if it.entries:
                    items.append(it)

    # ---- claude.ai export ----------------------------------------------
    if export_zip:
        groups = {}
        for rel, data, cat in read_export(export_zip):
            if cat not in categories:
                continue
            if cat not in groups:
                title = {"artifacts": "claude.ai artifacts (from export)",
                         "prompts": "claude.ai project instructions & prompt history",
                         "history": "claude.ai conversations (from export)"}[cat]
                groups[cat] = Item("export:" + cat, cat, title, None, "claude-ai/" + cat, "export")
            groups[cat].add_file(rel, data)
        items.extend(groups.values())

    return items


def _skill_description(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", text, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def _project_label(dirname):
    return dirname.replace("-", "/") if dirname.startswith("-") else dirname


def scan(env, accounts="current", export_zip=None):
    items = collect(env, CATEGORIES, accounts, export_zip)
    acct, org, email = env.current_account()
    summary = {c: {"items": 0, "files": 0, "bytes": 0, "description": d} for c, d in CATEGORIES.items()}
    for it in items:
        s = summary[it.category]
        s["items"] += 1
        s["files"] += len(it.entries)
        s["bytes"] += it.size()
    return {
        "current_account": {"account": acct, "org": org, "email": email},
        "accounts": env.accounts(),
        "roots": {k: str(v) for k, v in env.roots.items()},
        "desktop_running": env.desktop_running(),
        "categories": summary,
        "items": [_item_summary(it) for it in items],
    }


def _item_summary(it):
    return {"id": it.id, "category": it.category, "title": it.title, "files": len(it.entries),
            "bytes": it.size(), "restore": it.restore,
            "account": it.meta.get("account"), "description": it.meta.get("description", "")}


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------
def backup(env, categories=None, accounts="current", export_zip=None, out_dir=None,
           redact_secrets=False, label=None, progress=None, item_ids=None, keep=None):
    categories = sorted(set(categories or CATEGORIES))
    unknown = set(categories) - set(CATEGORIES)
    if unknown:
        raise BackupError("Unknown categories: %s" % ", ".join(sorted(unknown)))
    items = collect(env, categories, accounts, export_zip, redact_secrets)
    if item_ids is not None:
        wanted = set(item_ids)
        items = [it for it in items if it.id in wanted]
    if not items:
        raise BackupError("Nothing to back up for: %s" % ", ".join(categories))
    out_dir = Path(out_dir or default_output_dir(env))
    out_dir.mkdir(parents=True, exist_ok=True)
    if label is None:
        label = "full" if len(categories) == len(CATEGORIES) else "-".join(categories)
    path = out_dir / ("claude-backup-%s-%s%s" % (now_stamp(), re.sub(r"[^\w-]+", "_", label), EXT))

    acct, org, email = env.current_account()
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "tool_version": __version__,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": platform.node(),
        "platform": platform.system(),
        "categories": categories,
        "redacted": bool(redact_secrets),
        "source_account": {"account": acct, "org": org, "email": email},
        "accounts": [{"account": a, "org": o} for a, o in resolve_accounts(env, accounts)],
        "claude_ai_export": bool(export_zip),
        "items": [],
    }
    tmp = path.with_suffix(path.suffix + ".part")
    total = sum(len(it.entries) for it in items)
    done = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for it in items:
                files = []
                for rel, src in it.entries:
                    arc = "data/%s/%s" % (it.safe_id, rel)
                    h = hashlib.sha256()
                    if isinstance(src, bytes):
                        h.update(src)
                        zf.writestr(arc, src)
                        size = len(src)
                    else:
                        try:
                            zi = zipfile.ZipInfo.from_file(src, arc)
                            zi.compress_type = zipfile.ZIP_DEFLATED
                            with open(src, "rb") as f, zf.open(zi, "w", force_zip64=True) as w:
                                size = 0
                                for chunk in iter(lambda: f.read(1 << 20), b""):
                                    h.update(chunk)
                                    w.write(chunk)
                                    size += len(chunk)
                        except OSError:
                            continue  # file vanished or unreadable; skip it
                    files.append({"path": rel, "sha256": h.hexdigest(), "size": size})
                    done += 1
                    if progress:
                        progress(done, total, it.title)
                manifest["items"].append({
                    "id": it.id, "safe_id": it.safe_id, "category": it.category,
                    "title": it.title, "root": it.root, "rel": _tmpl(it.rel or "", *_ids(it, acct, org))
                    if it.rel is not None else None,
                    "restore": it.restore, "meta": it.meta, "files": files,
                })
            zf.writestr("manifest.json", _json_bytes(manifest))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if keep:
        prune(out_dir, keep)
    return path, manifest


def _ids(item, default_acct, default_org):
    return item.meta.get("account") or default_acct, item.meta.get("org") or default_org


# --------------------------------------------------------------------------
# Inspect
# --------------------------------------------------------------------------
def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise BackupError("%s is not a valid backup: %s" % (path, e))
    if manifest.get("format") != FORMAT:
        raise BackupError("%s is not a Claude backup" % path)
    if manifest.get("format_version", 0) > FORMAT_VERSION:
        raise BackupError("Backup was made by a newer version of this tool; please update")
    return manifest


def inspect(path):
    m = read_manifest(path)
    cats = {}
    for it in m["items"]:
        c = cats.setdefault(it["category"], {"items": 0, "files": 0, "bytes": 0})
        c["items"] += 1
        c["files"] += len(it["files"])
        c["bytes"] += sum(f["size"] for f in it["files"])
    return {
        "path": str(path),
        "created_at": m["created_at"], "host": m["host"], "platform": m["platform"],
        "tool_version": m["tool_version"], "redacted": m.get("redacted", False),
        "source_account": m.get("source_account"), "accounts": m.get("accounts", []),
        "categories": cats,
        "items": [{"id": i["id"], "category": i["category"], "title": i["title"],
                   "files": len(i["files"]), "bytes": sum(f["size"] for f in i["files"]),
                   "restore": i["restore"], "account": i["meta"].get("account"),
                   "description": i["meta"].get("description", "")} for i in m["items"]],
    }


def verify(path):
    """Check every file's checksum. Returns a list of problems (empty = OK)."""
    m = read_manifest(path)
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for it in m["items"]:
            for f in it["files"]:
                arc = "data/%s/%s" % (it["safe_id"], f["path"])
                if arc not in names:
                    problems.append("missing: " + arc)
                    continue
                h = hashlib.sha256()
                with zf.open(arc) as r:
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != f["sha256"]:
                    problems.append("checksum mismatch: " + arc)
    return problems


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------
class Op:
    def __init__(self, kind, dest, item_id, member=None, data=None, note=""):
        self.kind = kind      # write | merge-json | merge-claude-json | manifest-add | zip | out
        self.dest = Path(dest)
        self.item_id = item_id
        self.member = member  # (zip member, expected sha256) or list of them for "zip"
        self.data = data
        self.note = note
        self.exists = self.dest.exists()

    def describe(self):
        verb = {"write": "overwrite" if self.exists else "create",
                "out": "export", "zip": "build upload zip",
                "merge-json": "merge into" if self.exists else "create",
                "merge-claude-json": "merge into", "manifest-add": "register in",
                "skip": "keep existing"}[self.kind]
        return {"item": self.item_id, "action": verb, "path": str(self.dest),
                "exists": self.exists, "note": self.note}


def deep_merge(dst, src, overwrite=True):
    for k, v in src.items():
        if v == REDACTED and k in dst:
            continue
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v, overwrite)
        elif k not in dst or overwrite:
            dst[k] = copy.deepcopy(v)
    return dst


def _remap_bytes(data, mapping):
    if not mapping:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in mapping.items():
        text = text.replace(old, new)
    return text.encode("utf-8")


def plan_restore(env, path, item_ids=None, categories=None, target=None,
                 skill_targets=("claude-code", "upload"), conflict="overwrite", out_dir=None):
    """Work out every file operation a restore would perform, without doing any."""
    m = read_manifest(path)
    if target:
        t_acct, t_org = target
    else:
        t_acct, t_org, _ = env.current_account()
    src_default = m.get("source_account") or {}
    out_dir = Path(out_dir or default_output_dir(env)) / ("restored-" + now_stamp())
    skill_targets = set(skill_targets or ())
    ops, warnings = [], []
    needs_account = False

    selected = [i for i in m["items"]
                if (not item_ids or i["id"] in item_ids)
                and (not categories or i["category"] in categories)]

    for it in selected:
        files = it["files"]
        member = lambda f, it=it: ("data/%s/%s" % (it["safe_id"], f["path"]), f["sha256"])
        s_acct = it["meta"].get("account") or src_default.get("account")
        s_org = it["meta"].get("org") or src_default.get("org")
        mapping = {}
        if s_acct and t_acct and s_acct != t_acct:
            mapping[s_acct] = t_acct
        if s_org and t_org and s_org != t_org:
            mapping[s_org] = t_org
        rel = it["rel"]
        uses_account = rel is not None and ("{account}" in rel or "{org}" in rel)
        if uses_account and not (t_acct and t_org):
            needs_account = True
            continue
        if rel is not None:
            rel = rel.replace("{account}", t_acct or "").replace("{org}", t_org or "")
        root_ok = it["root"] in env.roots
        base = env.resolve(it["root"], rel) if (it["root"] and root_ok) else None

        kind = it["restore"]
        if kind == "export" or (it["root"] and not root_ok):
            if it["root"] and not root_ok:
                warnings.append("%s: %s does not exist on this machine; exported instead"
                                % (it["title"], it["root"]))
            dest_base = out_dir / (it["rel"] or it["safe_id"])
            for f in files:
                ops.append(Op("out", dest_base / f["path"], it["id"], member(f), mapping))
        elif kind == "skill":
            name = it["meta"].get("name") or it["title"]
            if "claude-code" in skill_targets:
                dest_base = env.claude_home / "skills" / name
                for f in files:
                    ops.append(Op("write", dest_base / f["path"], it["id"], member(f), mapping))
            if "upload" in skill_targets:
                ops.append(Op("zip", out_dir / "skills-to-upload" / (name + ".zip"), it["id"],
                              [(name + "/" + f["path"], member(f)) for f in files],
                              note="Upload in Claude: Settings > Capabilities > Skills"))
            if "desktop" in skill_targets and it["meta"].get("source") == "desktop":
                if not (t_acct and t_org):
                    needs_account = True
                else:
                    sdir = env.skills_dir(t_acct, t_org)
                    for f in files:
                        ops.append(Op("write", sdir / "skills" / name / f["path"], it["id"],
                                      member(f), mapping))
                    entry = dict(it["meta"].get("manifest_entry") or {
                        "name": name, "description": it["meta"].get("description", ""),
                        "creatorType": "user", "enabled": True})
                    ops.append(Op("manifest-add", sdir / "manifest.json", it["id"],
                                  data=("skills", entry),
                                  note="Local cache; Claude may replace it on next sync"))
        elif kind == "plugin":
            name = it["meta"].get("name") or it["safe_id"]
            ops.append(Op("zip", out_dir / "plugins-to-upload" / (name + ".zip"), it["id"],
                          [(f["path"], member(f)) for f in files],
                          note="Upload in Claude: Customize > Plugins > Upload"))
            if "desktop" in skill_targets and base is not None:
                for f in files:
                    ops.append(Op("write", base / f["path"], it["id"], member(f), mapping))
                ops.append(Op("manifest-add", base.parent / "manifest.json", it["id"],
                              data=("plugins", it["meta"].get("manifest_entry") or {"name": name})))
        elif kind == "json-merge":
            f = files[0]
            ops.append(Op("merge-json", base, it["id"], member(f), mapping))
        elif kind == "claude-json":
            f = files[0]
            ops.append(Op("merge-claude-json", base, it["id"], member(f), mapping))
        else:  # copy
            for f in files:
                dest = base if it["meta"].get("file") else base / f["path"]
                ops.append(Op("write", dest, it["id"], member(f), mapping))

    if conflict == "skip":
        for op in ops:
            if op.kind == "write" and op.exists:
                op.kind, op.note = "skip", "exists; skipped"
    if needs_account:
        warnings.append("Some items are tied to a Claude account, but no target account was "
                        "found. Sign in to Claude Desktop (or Claude Code) first, or pick a "
                        "target account.")
    touches_desktop = any(str(op.dest).startswith(str(env.desktop)) and op.kind not in ("skip", "out", "zip")
                          for op in ops)
    if touches_desktop and env.desktop_running():
        warnings.append("Claude Desktop is running. Quit it before restoring so it does not "
                        "overwrite the restored files.")
    return {"ops": ops, "warnings": warnings, "out_dir": out_dir,
            "target": {"account": t_acct, "org": t_org},
            "source": src_default}


def _read_member(zf, member):
    name, sha = member
    data = zf.read(name)
    if hashlib.sha256(data).hexdigest() != sha:
        raise BackupError("Backup is damaged: checksum mismatch for " + name)
    return data


class Snapshot:
    """Copies of every file a restore will change, so the restore can be undone."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.zf = zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        self.entries = []
        self.seen = set()

    def record(self, dest):
        dest = Path(dest)
        key = str(dest)
        if key in self.seen:
            return
        self.seen.add(key)
        if dest.is_file():
            arc = "files/%06d" % len(self.entries)
            self.zf.write(dest, arc)
            self.entries.append({"path": key, "existed": True, "arc": arc})
        else:
            self.entries.append({"path": key, "existed": False})

    def close(self):
        self.zf.writestr("snapshot.json", _json_bytes({
            "format": "claude-backup-snapshot", "created_at": now_stamp(), "entries": self.entries}))
        self.zf.close()


def _write_file(dest, data):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".cbtmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


def restore(env, path, item_ids=None, categories=None, target=None,
            skill_targets=("claude-code", "upload"), conflict="overwrite", out_dir=None,
            dry_run=False, progress=None):
    plan = plan_restore(env, path, item_ids, categories, target, skill_targets, conflict, out_dir)
    result = {"actions": [op.describe() for op in plan["ops"]], "warnings": plan["warnings"],
              "target": plan["target"], "source": plan["source"], "dry_run": dry_run,
              "out_dir": None, "snapshot": None}
    if dry_run:
        return result
    ops = [op for op in plan["ops"] if op.kind != "skip"]
    snap_dir = Path(out_dir or default_output_dir(env)) / "snapshots"
    snapshot = Snapshot(snap_dir / ("pre-restore-%s.zip" % now_stamp()))
    wrote_out = False
    try:
        with zipfile.ZipFile(path) as zf:
            for n, op in enumerate(ops, 1):
                if progress:
                    progress(n, len(ops), op.item_id)
                if op.kind == "out":
                    _write_file(op.dest, _remap_bytes(_read_member(zf, op.member), op.data))
                    wrote_out = True
                elif op.kind == "zip":
                    op.dest.parent.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(op.dest, "w", zipfile.ZIP_DEFLATED) as out:
                        for arcname, member in op.member:
                            out.writestr(arcname, _read_member(zf, member))
                    wrote_out = True
                elif op.kind == "write":
                    snapshot.record(op.dest)
                    _write_file(op.dest, _remap_bytes(_read_member(zf, op.member), op.data))
                elif op.kind == "merge-json":
                    incoming = json.loads(_remap_bytes(_read_member(zf, op.member), op.data))
                    current = _read_json(op.dest) if op.dest.exists() else {}
                    if not isinstance(current, dict):
                        current = {}
                    snapshot.record(op.dest)
                    _write_file(op.dest, _json_bytes(
                        deep_merge(current, incoming, overwrite=(conflict != "skip"))))
                elif op.kind == "merge-claude-json":
                    incoming = json.loads(_remap_bytes(_read_member(zf, op.member), op.data))
                    current = _read_json(op.dest) or {}
                    snapshot.record(op.dest)
                    over = conflict != "skip"
                    if "mcpServers" in incoming:
                        deep_merge(current.setdefault("mcpServers", {}), incoming["mcpServers"], over)
                    for p, sub in (incoming.get("projects") or {}).items():
                        deep_merge(current.setdefault("projects", {}).setdefault(p, {}), sub, over)
                    _write_file(op.dest, _json_bytes(current))
                elif op.kind == "manifest-add":
                    key, entry = op.data
                    current = _read_json(op.dest) or {}
                    lst = [e for e in current.get(key, []) if e.get("name") != entry.get("name")]
                    lst.append(entry)
                    current[key] = lst
                    snapshot.record(op.dest)
                    _write_file(op.dest, _json_bytes(current))
    finally:
        snapshot.close()
    if snapshot.entries:
        result["snapshot"] = str(snapshot.path)
    else:
        snapshot.path.unlink()
    if wrote_out:
        result["out_dir"] = str(plan["out_dir"])
    return result


def undo(snapshot_path):
    """Put back every file a restore changed, and remove files it created."""
    with zipfile.ZipFile(snapshot_path) as zf:
        meta = json.loads(zf.read("snapshot.json"))
        restored = removed = 0
        for e in reversed(meta["entries"]):
            p = Path(e["path"])
            if e["existed"]:
                _write_file(p, zf.read(e["arc"]))
                restored += 1
            elif p.exists():
                p.unlink()
                removed += 1
                parent = p.parent
                while parent != parent.parent and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
    return {"restored": restored, "removed": removed}


def list_backups(directory):
    directory = Path(directory)
    out = []
    if directory.is_dir():
        for p in sorted(directory.glob("*" + EXT), reverse=True):
            try:
                m = read_manifest(p)
            except BackupError:
                continue
            out.append({"path": str(p), "name": p.name, "size": p.stat().st_size,
                        "created_at": m["created_at"], "categories": m["categories"],
                        "items": len(m["items"]), "host": m["host"],
                        "email": (m.get("source_account") or {}).get("email")})
    return out


def list_snapshots(directory):
    d = Path(directory) / "snapshots"
    if not d.is_dir():
        return []
    return [{"path": str(p), "name": p.name, "size": p.stat().st_size}
            for p in sorted(d.glob("pre-restore-*.zip"), reverse=True)]


# --------------------------------------------------------------------------
# Migrate
# --------------------------------------------------------------------------
def migrate(env, from_pair, to_pair, categories=None, skill_targets=("claude-code", "upload", "desktop"),
            conflict="overwrite", out_dir=None, dry_run=False, export_zip=None, progress=None):
    """Copy one local account's data into another account (both on this machine).

    Always takes a backup of the source first, so the migration can be repeated
    or done again on a different machine.
    """
    path, _ = backup(env, categories, [tuple(from_pair)], export_zip, out_dir,
                     label="migrate-%s" % from_pair[0][:8], progress=progress)
    res = restore(env, path, categories=categories, target=tuple(to_pair),
                  skill_targets=skill_targets, conflict=conflict, out_dir=out_dir, dry_run=dry_run,
                  progress=progress)
    res["backup"] = str(path)
    return res


# --------------------------------------------------------------------------
# Library helpers (used by the app)
# --------------------------------------------------------------------------
def prune(directory, keep):
    """Keep the newest ``keep`` backups in a folder; move older ones to the trash."""
    backups = sorted(Path(directory).glob("claude-backup-*" + EXT), reverse=True)
    removed = []
    for p in backups[keep:]:
        delete_backup(p)
        removed.append(str(p))
    return removed


def delete_backup(path):
    """Move a backup to the system trash where possible, otherwise delete it."""
    path = Path(path)
    trash = Path.home() / ".Trash"
    if platform.system() == "Darwin" and trash.is_dir():
        dest = trash / path.name
        n = 1
        while dest.exists():
            dest = trash / ("%s %d%s" % (path.stem, n, path.suffix))
            n += 1
        try:
            shutil.move(str(path), str(dest))
            return str(dest)
        except OSError:
            pass
    path.unlink()
    return None


def item_files(path, item_id):
    m = read_manifest(path)
    for it in m["items"]:
        if it["id"] == item_id:
            return it
    raise BackupError("No item %s in backup" % item_id)


def read_file(path, item_id, rel, limit=200000):
    """Return (text or None, size) for previewing one file inside a backup."""
    it = item_files(path, item_id)
    with zipfile.ZipFile(path) as zf:
        data = zf.read("data/%s/%s" % (it["safe_id"], rel))
    try:
        return data[:limit].decode("utf-8"), len(data)
    except UnicodeDecodeError:
        return None, len(data)


def extract_items(path, item_ids, dest):
    """Copy items out of a backup as plain files, without restoring them."""
    m = read_manifest(path)
    dest = Path(dest)
    count = 0
    with zipfile.ZipFile(path) as zf:
        for it in m["items"]:
            if it["id"] not in item_ids:
                continue
            base = dest / it["safe_id"]
            for f in it["files"]:
                _write_file(base / f["path"],
                            _read_member(zf, ("data/%s/%s" % (it["safe_id"], f["path"]), f["sha256"])))
                count += 1
    return count


def last_backup(directory):
    b = list_backups(directory)
    return b[0] if b else None
