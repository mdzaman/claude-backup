"""Claude Backup in the browser: a local server on 127.0.0.1 plus a single-page app.

Every API call needs the per-launch token, which only the page served to the
person who launched the app knows. Long tasks run as background jobs that the
page polls for progress and can cancel. Only one server runs at a time;
launching again just reopens the page.
"""
import datetime as _dt
import json
import mimetypes
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, destinations as D, engine, mobile, schedule
from .claude_ai_export import read_export
from .settings import Settings

UI_FILE = Path(__file__).with_name("ui.html")
GUIDE_FILE = Path(__file__).with_name("docs") / "USER_GUIDE.md"
IDLE_SHUTDOWN = 15 * 60          # quit after this long with no open page and no job
CLAUDE_EXPORT_URL = "https://claude.ai/settings/data-privacy-controls"
CLAUDE_SKILLS_URL = "https://claude.ai/settings/capabilities"


# --------------------------------------------------------------------------
# OS helpers
# --------------------------------------------------------------------------
def reveal(path):
    path = Path(path)
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    elif system == "Windows":
        subprocess.run(["explorer", "/select,", str(path)] if path.is_file() else ["explorer", str(path)])
    else:
        subprocess.run(["xdg-open", str(path.parent if path.is_file() else path)])


def open_path(path):
    if platform.system() == "Darwin":
        subprocess.run(["open", str(path)])
    elif platform.system() == "Windows":
        os.startfile(str(path))  # noqa
    else:
        subprocess.run(["xdg-open", str(path)])


def open_terminal(command):
    """Run a command in a new Terminal window (macOS) so the user can interact with it."""
    if platform.system() == "Darwin":
        script = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate',
                        "-e", 'tell application "Terminal" to do script "%s"' % script])
    elif platform.system() == "Windows":
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", command])
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", command])
                break


def pick(kind):
    """Native file/folder chooser. Returns a path or None."""
    if platform.system() == "Darwin":
        script = {
            "folder": 'POSIX path of (choose folder with prompt "Choose a folder")',
            "backup": 'POSIX path of (choose file with prompt "Choose a Claude backup (.claudebackup)")',
            "export": 'POSIX path of (choose file with prompt "Choose your claude.ai data export (.zip)")',
        }.get(kind, 'POSIX path of (choose file)')
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to activate',
                            "-e", script], capture_output=True, text=True)
        return r.stdout.strip() or None
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askdirectory() if kind == "folder" else filedialog.askopenfilename()
        root.destroy()
        return p or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------
class Job:
    def __init__(self, title):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.state = "running"
        self.done = self.total = 0
        self.label = ""
        self.result = None
        self.error = None
        self.cancel = threading.Event()

    def progress(self, done, total, label):
        if self.cancel.is_set():
            raise engine.Cancelled("Cancelled")
        self.done, self.total, self.label = done, total, label

    def to_json(self):
        return {"id": self.id, "title": self.title, "state": self.state, "done": self.done,
                "total": self.total, "label": self.label, "result": self.result, "error": self.error}


class Server:
    def __init__(self, env):
        self.env = env
        self.settings = Settings(env)
        self.token = secrets.token_urlsafe(24)
        self.jobs = {}
        self.exports = {}            # zip path -> {"index": [...], "data": {rel: bytes}, "prompts": n}
        self.last_seen = time.time()
        self.httpd = None
        self.pairing = mobile.Pairing(self.settings.path.parent)
        self.mobile_httpd = None
        self.mobile_token = None
        self.mobile_url = None

    # -- jobs ----------------------------------------------------------------
    def start(self, title, fn, on_done=None):
        if any(j.state == "running" for j in self.jobs.values()):
            raise engine.BackupError("Another task is still running. Wait for it to finish or cancel it.")
        job = Job(title)
        self.jobs[job.id] = job

        def run():
            try:
                job.result = fn(job.progress)
                job.state = "done"
                if on_done:
                    on_done(job.result)
            except engine.Cancelled:
                job.state = "cancelled"
            except Exception as e:
                traceback.print_exc()
                job.state, job.error = "error", "%s" % e if isinstance(e, (engine.BackupError, D.DestinationError)) else \
                    "%s: %s" % (type(e).__name__, e)
                self.log("Error in %s: %s" % (title, job.error))
        threading.Thread(target=run, daemon=True).start()
        return {"job": job.id}

    # -- activity log ----------------------------------------------------------
    @property
    def log_file(self):
        return self.settings.path.parent / "activity.log"

    def log(self, text):
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    def out(self, b=None):
        return str((b or {}).get("out") or self.settings.out_dir)

    # -- routes ----------------------------------------------------------------
    def route(self, path, b):
        env, st = self.env, self.settings
        if path == "/api/heartbeat":
            return {"ok": True}

        if path == "/api/scan":
            r = engine.scan(env, b.get("accounts") or st["accounts"])
            r.update(default_out=str(st.out_dir), categories_info=engine.CATEGORIES,
                     skill_targets=engine.SKILL_TARGETS, version=__version__,
                     python=platform.python_version(), schedule=schedule.describe(schedule.status()),
                     last_backup=engine.last_backup(st.out_dir), settings=st.data,
                     export_url=CLAUDE_EXPORT_URL, skills_url=CLAUDE_SKILLS_URL)
            return r

        # backup / restore / migrate ---------------------------------------------
        if path == "/api/backup":
            accounts = b.get("accounts") or st["accounts"]
            if isinstance(accounts, list):
                accounts = [tuple(a) for a in accounts]
            out = self.out(b)
            args = (b.get("categories") or None, accounts, b.get("export") or None, out,
                    bool(b.get("redact", st["redact"])))

            def fn(progress):
                p, m = engine.backup(env, *args, label=b.get("label"), progress=progress,
                                     item_ids=b.get("items"), keep=b.get("keep"))
                copies = []
                if b.get("upload", True):
                    copies = D.copy_to(st["destinations"], p, st.secrets, progress)
                return {"path": str(p), "name": p.name, "items": len(m["items"]), "size": p.stat().st_size,
                        "copies": copies}

            def done(r):
                self.log("Backup created: %s (%d items, %s)" % (r["name"], r["items"], engine.human_size(r["size"])))
                for c in r["copies"]:
                    self.log("  %s %s: %s" % ("copied to" if c["ok"] else "FAILED copying to", c["name"], c["message"]))
            return self.start("Backing up", fn, done)

        if path == "/api/restore":
            target = tuple(b["target"]) if b.get("target") else None
            dry = bool(b.get("dry_run"))

            def fn(progress):
                return engine.restore(env, b["path"], b.get("items") or None, b.get("categories") or None,
                                      target, b.get("skills") or [], b.get("conflict", "overwrite"),
                                      self.out(b), dry, progress)
            return self.start("Planning restore" if dry else "Restoring", fn, None if dry else lambda r: self.log(
                "Restored %s into account %s" % (Path(b["path"]).name, (r["target"]["account"] or "")[:8])))

        if path == "/api/migrate":
            dry = bool(b.get("dry_run"))

            def fn(progress):
                return engine.migrate(env, tuple(b["from"]), tuple(b["to"]), b.get("categories") or None,
                                      b.get("skills") or [], b.get("conflict", "overwrite"), self.out(b),
                                      dry, b.get("export") or None, progress)
            return self.start("Planning migration" if dry else "Migrating", fn, None if dry else lambda r: self.log(
                "Migrated account %s into %s" % (b["from"][0][:8], b["to"][0][:8])))

        if path == "/api/job":
            job = self.jobs.get(b.get("id"))
            if not job:
                raise engine.BackupError("Unknown task")
            return job.to_json()
        if path == "/api/job/cancel":
            job = self.jobs.get(b.get("id"))
            if job:
                job.cancel.set()
            return {"ok": True}

        # library -------------------------------------------------------------------
        if path == "/api/backups":
            return {"backups": engine.list_backups(self.out(b)), "out": self.out(b)}
        if path == "/api/inspect":
            return engine.inspect(b["path"])
        if path == "/api/contents":
            m = engine.read_manifest(b["path"])
            return {"created_at": m["created_at"], "host": m["host"], "platform": m["platform"],
                    "tool_version": m["tool_version"], "redacted": m.get("redacted", False),
                    "source_account": m.get("source_account"),
                    "items": [{"id": i["id"], "category": i["category"], "title": i["title"],
                               "files": [{"path": f["path"], "size": f["size"]} for f in i["files"][:2000]],
                               "nfiles": len(i["files"]), "bytes": sum(f["size"] for f in i["files"])}
                              for i in m["items"]]}
        if path == "/api/file":
            text, size = engine.read_file(b["path"], b["item"], b["file"])
            return {"text": text, "size": size}
        if path == "/api/verify":
            return self.start("Verifying", lambda progress: {"problems": engine.verify(b["path"])})
        if path == "/api/extract":
            dest = b.get("dest") or pick("folder")
            if not dest:
                return {"cancelled": True}
            n = engine.extract_items(b["path"], set(b["items"]), dest)
            self.log("Extracted %d files from %s" % (n, Path(b["path"]).name))
            reveal(dest)
            return {"files": n, "dest": dest}
        if path == "/api/delete-backup":
            engine.read_manifest(b["path"])          # only ever delete real backups
            where = engine.delete_backup(b["path"])
            self.log("Moved to Trash: " + Path(b["path"]).name)
            return {"trash": where}
        if path == "/api/import-backup":
            src = b.get("src") or pick("backup")
            if not src:
                return {"cancelled": True}
            engine.read_manifest(src)
            dest = Path(self.out(b)) / Path(src).name
            if dest.resolve() != Path(src).resolve():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            return {"path": str(dest)}

        # artifacts -----------------------------------------------------------------
        if path == "/api/artifacts/load":
            zp = b["zip"]
            if not zipfile.is_zipfile(zp):
                raise engine.BackupError("That file isn't a claude.ai export .zip.")

            def fn(progress):
                entries = read_export(zp)
                data = {rel: d for rel, d, cat in entries if cat == "artifacts"}
                index = json.loads(data.pop("index.json", b"[]"))
                self.exports[zp] = {"index": index, "data": data}
                return {"artifacts": index, "prompts": sum(1 for _, _, c in entries if c == "prompts")}
            return self.start("Reading claude.ai export", fn, lambda r: self.log(
                "Loaded claude.ai export: %d artifacts" % len(r["artifacts"])))
        if path == "/api/artifacts/file":
            data = self._export(b["zip"])["data"][b["file"]]
            return {"text": data[:300000].decode("utf-8", "replace"), "size": len(data)}
        if path == "/api/artifacts/save":
            ex = self._export(b["zip"])
            files = b.get("files") or list(ex["data"])
            dest = b.get("dest") or pick("folder")
            if not dest:
                return {"cancelled": True}
            for f in files:
                p = Path(dest) / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(ex["data"][f])
            self.log("Saved %d artifacts to %s" % (len(files), dest))
            reveal(dest)
            return {"saved": len(files), "dest": dest}
        if path == "/api/artifacts/open":
            data = self._export(b["zip"])["data"][b["file"]]
            p = Path(tempfile.mkdtemp(prefix="claude-artifact-")) / Path(b["file"]).name
            p.write_bytes(data)
            open_path(p)
            return {"ok": True}

        # destinations (drives & clouds) ------------------------------------------------
        if path == "/api/destinations":
            return {"destinations": [self._public(d) for d in st["destinations"]],
                    "detected": D.detect_locations(env.home), "kinds": D.KINDS,
                    "rclone": D.rclone_path(), "rclone_remotes": D.rclone_remotes() if D.rclone_path() else [],
                    "secrets_store": "macOS Keychain" if st.secrets.keychain else str(st.secrets.file)}
        if path == "/api/destinations/save":
            return self._save_destination(b)
        if path == "/api/destinations/remove":
            cfg = st.destination(b["id"])
            st["destinations"] = [d for d in st["destinations"] if d["id"] != cfg["id"]]
            st.secrets.delete(cfg["id"])
            self.log("Removed destination " + cfg["name"])
            return {"ok": True}
        if path == "/api/destinations/test":
            cfg = st.destination(b["id"])
            return self.start("Testing " + cfg["name"], lambda progress: {"message": D.make(cfg, st.secrets).test()})
        if path == "/api/destinations/list":
            cfg = st.destination(b["id"])
            return self.start("Listing " + cfg["name"], lambda progress: {"backups": D.make(cfg, st.secrets).list()})
        if path == "/api/destinations/upload":
            cfg = st.destination(b["id"])
            engine.read_manifest(b["path"])

            def fn(progress):
                dest = D.make(cfg, st.secrets)
                dest.upload(b["path"], progress)
                return {"removed": dest.prune(cfg.get("keep") or 0)}
            return self.start("Copying to " + cfg["name"], fn,
                              lambda r: self.log("Copied %s to %s" % (Path(b["path"]).name, cfg["name"])))
        if path == "/api/destinations/download":
            cfg = st.destination(b["id"])

            def fn(progress):
                p = D.make(cfg, st.secrets).download(b["name"], self.out(b), progress)
                engine.read_manifest(p)
                return {"path": str(p)}
            return self.start("Downloading from " + cfg["name"], fn,
                              lambda r: self.log("Downloaded %s from %s" % (b["name"], cfg["name"])))
        if path == "/api/destinations/delete":
            cfg = st.destination(b["id"])
            D.make(cfg, st.secrets).delete(b["name"])
            self.log("Deleted %s from %s" % (b["name"], cfg["name"]))
            return {"ok": True}
        if path == "/api/rclone/config":
            exe = D.rclone_path()
            if not exe:
                raise engine.BackupError("rclone isn't installed yet.")
            open_terminal("%s config" % exe)
            return {"ok": True}

        # schedule ------------------------------------------------------------------
        if path == "/api/schedule":
            s = schedule.status()
            s["describe"] = schedule.describe(s)
            s["weekdays"] = schedule.WEEKDAYS
            s["log_exists"] = schedule.log_path(s.get("out_dir") or st.out_dir).exists()
            return s
        if path == "/api/schedule/save":
            cats = b.get("categories") or list(engine.CATEGORIES)
            schedule.install(b["frequency"], int(b["hour"]), int(b["minute"]), int(b.get("weekday", 0)),
                             None if len(cats) == len(engine.CATEGORIES) else cats,
                             b.get("out") or st.out_dir, int(b["keep"]), b.get("accounts", "current"))
            st["keep"] = int(b["keep"])
            s = schedule.status()
            self.log("Automatic backups: " + schedule.describe(s))
            return {"describe": schedule.describe(s)}
        if path == "/api/schedule/off":
            schedule.uninstall()
            self.log("Automatic backups turned off")
            return {"describe": "Off"}
        if path == "/api/schedule/log":
            p = schedule.log_path(b.get("out") or st.out_dir)
            return {"text": p.read_text(encoding="utf-8", errors="replace")[-20000:] if p.exists() else ""}

        # undo & activity ----------------------------------------------------------
        if path == "/api/snapshots":
            snaps = []
            for s in engine.list_snapshots(self.out(b)):
                try:
                    with zipfile.ZipFile(s["path"]) as z:
                        s["files"] = len(json.loads(z.read("snapshot.json"))["entries"])
                except Exception:
                    s["files"] = None
                snaps.append(s)
            return {"snapshots": snaps}
        if path == "/api/snapshot":
            with zipfile.ZipFile(b["path"]) as z:
                entries = json.loads(z.read("snapshot.json"))["entries"]
            return {"entries": [{"path": e["path"], "existed": e["existed"]} for e in entries]}
        if path == "/api/undo":
            r = engine.undo(b["snapshot"])
            self.log("Undid restore %s: %d put back, %d removed" % (Path(b["snapshot"]).name, r["restored"], r["removed"]))
            return r
        if path == "/api/snapshot/delete":
            p = Path(b["path"])
            if p.parent.name == "snapshots" and p.name.startswith("pre-restore-"):
                p.unlink()
            return {"ok": True}
        if path == "/api/activity":
            try:
                lines = self.log_file.read_text(encoding="utf-8").splitlines()[-500:]
            except OSError:
                lines = []
            return {"lines": lines}

        # upload checklist --------------------------------------------------------
        if path == "/api/uploads":
            out = Path(b["out_dir"])
            done = set(st["uploaded"].get(str(out), []))
            files = []
            for kind in ("skills-to-upload", "plugins-to-upload"):
                for z in sorted(out.glob(kind + "/*.zip")):
                    files.append({"path": str(z), "name": z.stem, "kind": kind.split("-")[0][:-1],
                                  "done": str(z) in done})
            return {"files": files}
        if path == "/api/uploads/mark":
            up = dict(st["uploaded"])
            cur = set(up.get(b["out_dir"], []))
            (cur.add if b["done"] else cur.discard)(b["file"])
            up[b["out_dir"]] = sorted(cur)
            st["uploaded"] = up
            return {"ok": True}

        # settings & misc -------------------------------------------------------------
        if path == "/api/settings":
            for k in ("out_dir", "skill_targets", "redact", "accounts"):
                if k in b:
                    st[k] = b[k]
            return {"settings": st.data, "out": str(st.out_dir),
                    "roots": {k: str(v) for k, v in env.roots.items()},
                    "settings_file": str(st.path)}
        if path == "/api/mobile":
            return self.mobile_status()
        if path == "/api/mobile/start":
            self.start_mobile()
            return self.mobile_status()
        if path == "/api/mobile/stop":
            self.stop_mobile()
            self.log("Phone access turned off")
            return self.mobile_status()
        if path == "/api/mobile/reset":
            was_on = bool(self.mobile_httpd)
            self.stop_mobile()
            self.pairing.rotate()
            self.log("Paired phones disconnected")
            if was_on:
                self.start_mobile()
            return self.mobile_status()
        if path == "/api/help":
            return {"text": GUIDE_FILE.read_text(encoding="utf-8") if GUIDE_FILE.exists() else "The user guide is missing."}
        if path == "/api/pick":
            return {"path": pick(b.get("kind", "backup"))}
        if path == "/api/reveal":
            reveal(b["path"])
            return {"ok": True}
        if path == "/api/open":
            open_path(b["path"])
            return {"ok": True}
        if path == "/api/quit":
            threading.Timer(0.3, self.shutdown).start()
            return {"ok": True}
        raise engine.BackupError("Unknown endpoint " + path)

    SECRET_FIELDS = {"s3": "secret_key", "azure": "sas"}

    def _public(self, d):
        out = {k: v for k, v in d.items()}
        out["has_secret"] = bool(self.settings.secrets.get(d["id"])) if d["type"] in self.SECRET_FIELDS else None
        return out

    def _save_destination(self, b):
        st = self.settings
        kind = b.get("kind") or "folder"
        if kind not in D.KINDS:
            raise engine.BackupError("Unknown destination kind")
        dtype = D.KINDS[kind]["type"]
        allowed = {"folder": ("path", "subfolder"), "s3": ("bucket", "prefix", "region", "endpoint", "profile", "access_key"),
                   "azure": ("container_url", "prefix"), "rclone": ("remote",)}[dtype]
        dests = list(st["destinations"])
        existing = next((d for d in dests if d["id"] == b.get("id")), None) if b.get("id") else None
        cfg = {"id": existing["id"] if existing else uuid.uuid4().hex[:10], "type": dtype, "kind": kind,
               "name": (b.get("name") or D.KINDS[kind]["label"]).strip()[:60],
               "auto": bool(b.get("auto")), "keep": int(b.get("keep") or 0)}
        for k in allowed:
            v = b.get(k)
            if isinstance(v, str):
                v = v.strip()
            if v not in (None, ""):
                cfg[k] = v
        if dtype == "azure" and "?" in cfg.get("container_url", ""):
            cfg["container_url"], sas = cfg["container_url"].split("?", 1)
            b["sas"] = b.get("sas") or sas
        if any(d["name"].lower() == cfg["name"].lower() and d["id"] != cfg["id"] for d in dests):
            raise engine.BackupError("Another destination is already called %s." % cfg["name"])
        secret = (b.get(self.SECRET_FIELDS.get(dtype, "")) or "").strip()
        if secret:
            st.secrets.set(cfg["id"], secret.lstrip("?"))
        dests = [cfg if d["id"] == cfg["id"] else d for d in dests] if existing else dests + [cfg]
        st["destinations"] = dests
        self.log(("Updated" if existing else "Added") + " destination " + cfg["name"])
        return {"destination": self._public(cfg)}

    # -- phone access -----------------------------------------------------------
    def start_mobile(self):
        if self.mobile_httpd:
            return
        ip = mobile.lan_ip()
        if not ip:
            raise engine.BackupError("This computer isn't connected to a network.")
        self.pairing.ensure_certificate(ip)
        self.mobile_token = self.pairing.token()
        handler = make_handler(self, remote=True)
        try:
            httpd = QuietServer(("0.0.0.0", mobile.DEFAULT_PORT), handler)
        except OSError:
            httpd = QuietServer(("0.0.0.0", 0), handler)
        httpd.socket = self.pairing.ssl_context().wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.mobile_httpd = httpd
        self.mobile_url = "https://%s:%d/?t=%s" % (ip, httpd.server_address[1], self.mobile_token)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.log("Phone access turned on at %s:%d" % (ip, httpd.server_address[1]))

    def stop_mobile(self):
        httpd, self.mobile_httpd = self.mobile_httpd, None
        self.mobile_token = self.mobile_url = None
        if httpd:
            httpd.shutdown()
            httpd.server_close()

    def mobile_status(self):
        on = bool(self.mobile_httpd)
        return {"on": on, "url": self.mobile_url, "qr": mobile.qr_svg(self.mobile_url) if on else None}

    def _export(self, zp):
        if zp not in self.exports:
            raise engine.BackupError("Load the export first.")
        return self.exports[zp]

    def shutdown(self):
        self.stop_mobile()
        try:
            state_file().unlink()
        except OSError:
            pass
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def watch_idle(self):
        while True:
            time.sleep(30)
            busy = self.mobile_httpd or any(j.state == "running" for j in self.jobs.values())
            if not busy and time.time() - self.last_seen > IDLE_SHUTDOWN:
                print("No open page for a while; stopping.", flush=True)
                self.shutdown()
                return


class QuietServer(ThreadingHTTPServer):
    """Phone listener: failed TLS handshakes (self-signed cert warnings) are routine."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


ICON_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="110" '
            b'fill="#c2410c"/><rect x="110" y="150" width="292" height="64" rx="18" fill="#fff"/><rect x="130" y="224" '
            b'width="252" height="168" rx="18" fill="#fff"/><rect x="212" y="262" width="88" height="28" rx="14" '
            b'fill="#c2410c"/></svg>')
_touch_icon = {}


def touch_icon():
    if "png" not in _touch_icon:
        from .macapp import icon_png
        _touch_icon["png"] = icon_png(180)
    return _touch_icon["png"]


def make_handler(server, remote=False):
    class Handler(BaseHTTPRequestHandler):
        timeout = 60 if remote else None

        def _token(self):
            return (server.mobile_token if remote else server.token) or ""

        def _client_ok(self):
            if remote and not mobile.is_private_client(self.client_address[0]):
                self._send(403, b"Forbidden", "text/plain")
                return False
            return True

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            if not self._client_ok():
                return None
            if url.path == "/ping":
                return self._send(200, {"app": "claude-backup", "version": __version__})
            if url.path == "/icon.svg":
                return self._send(200, ICON_SVG, "image/svg+xml")
            if url.path == "/icon-180.png":
                return self._send(200, touch_icon(), "image/png")
            token = self._token()
            if not token or not secrets.compare_digest(qs.get("t", [""])[0].encode(), token.encode()):
                msg = b"Scan the QR code on your computer again." if remote else b"Open Claude Backup from its app icon or launcher."
                return self._send(403, msg, "text/plain")
            server.last_seen = time.time()
            if url.path == "/manifest.webmanifest":
                return self._send(200, {
                    "name": "Claude Backup", "short_name": "Claude Backup", "display": "standalone",
                    "start_url": "/?t=" + token, "scope": "/", "background_color": "#f6f5f1", "theme_color": "#c2410c",
                    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                              {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"}],
                }, "application/manifest+json")
            if url.path == "/":
                html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", token) \
                    .replace("__VERSION__", __version__).replace("__REMOTE__", "1" if remote else "0")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if url.path == "/artifact" and not remote:
                # View an artifact in its own sandboxed origin: it can't reach the API.
                zp, f = qs.get("zip", [""])[0], qs.get("file", [""])[0]
                try:
                    data = server._export(zp)["data"][f]
                except (engine.BackupError, KeyError):
                    return self._send(404, b"Not found", "text/plain")
                ctype = mimetypes.guess_type(f)[0] or "text/plain"
                if ctype.startswith("text/") or ctype in ("image/svg+xml", "application/javascript"):
                    ctype += "; charset=utf-8"
                if f.endswith((".jsx", ".tsx", ".py", ".mmd", ".md", ".ts")):
                    ctype = "text/plain; charset=utf-8"
                return self._send(200, data, ctype, {"Content-Security-Policy": "sandbox allow-scripts"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._client_ok():
                return None
            token = self._token()
            if not token or not secrets.compare_digest(self.headers.get("X-Token", "").encode(), token.encode()):
                return self._send(403, {"error": "forbidden"})
            server.last_seen = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            path = urlparse(self.path).path
            try:
                if remote:
                    body = mobile.sanitize_remote(path, body, server.settings.out_dir)
                return self._send(200, server.route(path, body))
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (engine.BackupError, D.DestinationError, FileNotFoundError, KeyError) as e:
                msg = str(e) if isinstance(e, engine.BackupError) else "Not found: %s" % e
                return self._send(400, {"error": msg})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    return Handler


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------
def state_file():
    return Path.home() / ".claude-backup" / "server.json"


def running_url():
    """URL of an already-running server, or None."""
    try:
        s = json.loads(state_file().read_text())
        with urllib.request.urlopen("http://127.0.0.1:%d/ping" % s["port"], timeout=1.5) as r:
            if json.loads(r.read()).get("app") == "claude-backup":
                return "http://127.0.0.1:%d/?t=%s" % (s["port"], s["token"])
    except Exception:
        pass
    return None


def serve(env, port=0, open_browser=True, reuse=True):
    if reuse:
        url = running_url()
        if url:
            print("Claude Backup is already running:\n  %s" % url, flush=True)
            if open_browser:
                webbrowser.open(url)
            return None
    server = Server(env)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(server))
    server.httpd = httpd
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/?t=%s" % (port, server.token)
    if reuse:
        sf = state_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"port": port, "token": server.token, "pid": os.getpid()}))
        os.chmod(sf, 0o600)
    print("Claude Backup is running at:\n  %s\nPress Ctrl+C to quit." % url, flush=True)
    threading.Thread(target=server.watch_idle, daemon=True).start()
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        if reuse:
            try:
                if json.loads(state_file().read_text()).get("pid") == os.getpid():
                    state_file().unlink()
            except (OSError, ValueError):
                pass
    return server
