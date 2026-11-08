"""End-to-end tests of the browser app's HTTP API against fake Claude installs."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from claude_backup import web
from claude_backup.env import Env

from test_engine import ACCT_A, ACCT_B, ORG_A, ORG_B, make_export, make_home


class Client:
    def __init__(self, env):
        self.server = web.Server(env)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.server))
        self.server.httpd = self.httpd
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def raw(self, path, body=None, token=True, method="POST"):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Token": self.server.token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def api(self, path, body=None):
        code, _, data = self.raw(path, body)
        out = json.loads(data)
        if code != 200:
            raise AssertionError("%s -> %s %s" % (path, code, out))
        return out

    def job(self, path, body=None):
        jid = self.api(path, body)["job"]
        for _ in range(500):
            j = self.api("/api/job", {"id": jid})
            if j["state"] != "running":
                if j["state"] != "done":
                    raise AssertionError("job %s: %s" % (j["state"], j["error"]))
                return j["result"]
            time.sleep(0.02)
        raise AssertionError("job timed out")


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home_a, self.desk_a = make_home(base / "a", ACCT_A, ORG_A, "a@example.com")
        self.home_b, self.desk_b = make_home(base / "b", ACCT_B, ORG_B, "b@example.com", with_data=False)
        for p in (mock.patch.object(web, "reveal", lambda p: None),
                  mock.patch.object(web, "open_path", lambda p: None),
                  mock.patch.object(Env, "desktop_running", lambda self: False)):
            p.start()
            self.addCleanup(p.stop)
        self.a = Client(Env(home=self.home_a, desktop=self.desk_a))
        self.addCleanup(self.a.close)

    def tearDown(self):
        self.tmp.cleanup()

    def test_security(self):
        code, _, _ = self.a.raw("/api/scan", token=False)
        self.assertEqual(code, 403)
        code, _, _ = self.a.raw("/?t=wrong", method="GET")
        self.assertEqual(code, 403)
        code, headers, html = self.a.raw("/?t=" + self.a.server.token, method="GET")
        self.assertEqual(code, 200)
        self.assertIn(self.a.server.token.encode(), html)
        code, _, body = self.a.raw("/ping", method="GET")
        self.assertEqual(json.loads(body)["app"], "claude-backup")
        code, _, body = self.a.raw("/api/delete-backup", {"path": str(self.home_a / ".claude" / "CLAUDE.md")})
        self.assertEqual(code, 400)                     # refuses to delete anything that isn't a backup
        self.assertTrue((self.home_a / ".claude" / "CLAUDE.md").exists())

    def test_backup_library_restore_undo(self):
        a = self.a
        scan = a.api("/api/scan")
        self.assertEqual(scan["current_account"]["account"], ACCT_A)
        ids = [i["id"] for i in scan["items"] if i["category"] != "history"]
        r = a.job("/api/backup", {"categories": ["skills", "plugins", "prompts", "settings"], "items": ids})
        path = r["path"]
        backups = a.api("/api/backups")["backups"]
        self.assertEqual([b["path"] for b in backups], [path])
        self.assertNotIn("history", backups[0]["categories"])

        contents = a.api("/api/contents", {"path": path})
        self.assertIn("skill:my-skill", [i["id"] for i in contents["items"]])
        f = a.api("/api/file", {"path": path, "item": "skill:my-skill", "file": "SKILL.md"})
        self.assertIn("Do it.", f["text"])
        self.assertEqual(a.job("/api/verify", {"path": path})["problems"], [])
        dest = Path(self.tmp.name) / "extracted"
        self.assertGreater(a.api("/api/extract", {"path": path, "items": ["skill:my-skill"], "dest": str(dest)})["files"], 0)

        # Restore into account B on another "machine"
        b = Client(Env(home=self.home_b, desktop=self.desk_b))
        self.addCleanup(b.close)
        self.assertEqual(b.api("/api/scan")["current_account"]["account"], ACCT_B)
        plan = b.job("/api/restore", {"path": path, "skills": ["claude-code", "upload"], "dry_run": True})
        self.assertTrue(plan["dry_run"])
        self.assertFalse((self.home_b / ".claude" / "CLAUDE.md").exists())
        res = b.job("/api/restore", {"path": path, "skills": ["claude-code", "upload"]})
        self.assertEqual(res["target"]["account"], ACCT_B)
        self.assertEqual((self.home_b / ".claude" / "CLAUDE.md").read_text(), "Be terse.")

        uploads = b.api("/api/uploads", {"out_dir": res["out_dir"]})["files"]
        self.assertEqual({u["name"] for u in uploads if u["kind"] == "skill"}, {"my-skill", "cc-skill"})
        b.api("/api/uploads/mark", {"out_dir": res["out_dir"], "file": uploads[0]["path"], "done": True})
        self.assertTrue(b.api("/api/uploads", {"out_dir": res["out_dir"]})["files"][0]["done"])

        snaps = b.api("/api/snapshots")["snapshots"]
        self.assertEqual(len(snaps), 1)
        changed = b.api("/api/snapshot", {"path": snaps[0]["path"]})["entries"]
        self.assertTrue(any(e["path"].endswith("CLAUDE.md") for e in changed))
        b.api("/api/undo", {"snapshot": snaps[0]["path"]})
        self.assertFalse((self.home_b / ".claude" / "CLAUDE.md").exists())
        log = "\n".join(b.api("/api/activity")["lines"])
        self.assertIn("Restored", log)
        self.assertIn("Undid restore", log)


    def test_migrate_and_cancel(self):
        from test_engine import write
        write(self.desk_a / "local-agent-mode-sessions" / ACCT_B / ORG_B / "scheduled-tasks.json", "{}")
        res = self.a.job("/api/migrate", {"from": [ACCT_A, ORG_A], "to": [ACCT_B, ORG_B],
                                          "categories": ["skills"], "skills": ["desktop", "upload"]})
        sdir = Env(home=self.home_a, desktop=self.desk_a).skills_dir(ACCT_B, ORG_B)
        self.assertTrue((sdir / "skills" / "my-skill" / "SKILL.md").exists())
        self.assertTrue(Path(res["backup"]).exists())
        # A long backup: only one task at a time, and Cancel stops it
        def long_backup(*a, progress=None, **k):
            for n in range(500):
                progress(n, 500, "file %d" % n)
                time.sleep(0.01)
            raise AssertionError("was not cancelled")
        with mock.patch("claude_backup.engine.backup", side_effect=long_backup):
            jid = self.a.api("/api/backup", {})["job"]
            code, _, body = self.a.raw("/api/backup", {})
            self.assertEqual(code, 400)
            self.assertIn("still running", json.loads(body)["error"])
            self.a.api("/api/job/cancel", {"id": jid})
            for _ in range(100):
                if self.a.api("/api/job", {"id": jid})["state"] != "running":
                    break
                time.sleep(0.02)
            self.assertEqual(self.a.api("/api/job", {"id": jid})["state"], "cancelled")

    def test_artifacts(self):
        exp = Path(self.tmp.name) / "export.zip"
        make_export(exp)
        r = self.a.job("/api/artifacts/load", {"zip": str(exp)})
        files = {x["title"]: x["file"] for x in r["artifacts"]}
        self.assertEqual(set(files), {"Landing Page", "Calc", "Notes"})
        self.assertEqual(self.a.api("/api/artifacts/file", {"zip": str(exp), "file": files["Landing Page"]})["text"],
                         "<h1>Hello world</h1>")
        from urllib.parse import quote
        code, headers, body = self.a.raw("/artifact?t=%s&zip=%s&file=%s" % (
            self.a.server.token, quote(str(exp)), quote(files["Landing Page"])), method="GET")
        self.assertEqual(body, b"<h1>Hello world</h1>")
        self.assertIn("sandbox", headers["Content-Security-Policy"])   # can't reach the API
        dest = Path(self.tmp.name) / "arts"
        self.assertEqual(self.a.api("/api/artifacts/save", {"zip": str(exp), "dest": str(dest)})["saved"], 3)

    def test_destinations_flow(self):
        os.environ["CLAUDE_BACKUP_SECRETS"] = "file"
        self.addCleanup(os.environ.pop, "CLAUDE_BACKUP_SECRETS", None)
        drive = Path(self.tmp.name) / "usb"
        drive.mkdir()
        a = self.a
        info = a.api("/api/destinations")
        self.assertIn("s3", info["kinds"])
        d = a.api("/api/destinations/save", {"kind": "external", "name": "USB", "path": str(drive),
                                             "auto": True, "keep": 2})["destination"]
        self.assertEqual(a.job("/api/destinations/test", {"id": d["id"]})["message"][:6], "Ready.")
        r = a.job("/api/backup", {"categories": ["prompts"]})
        self.assertEqual([(c["name"], c["ok"]) for c in r["copies"]], [("USB", True)])
        listed = a.job("/api/destinations/list", {"id": d["id"]})["backups"]
        self.assertEqual([b["name"] for b in listed], [r["name"]])
        # Download into a fresh library folder, then delete from the drive
        other = Path(self.tmp.name) / "lib2"
        got = a.job("/api/destinations/download", {"id": d["id"], "name": r["name"], "out": str(other)})
        self.assertTrue(Path(got["path"]).exists())
        a.api("/api/destinations/delete", {"id": d["id"], "name": r["name"]})
        self.assertEqual(a.job("/api/destinations/list", {"id": d["id"]})["backups"], [])
        # Secrets never land in the settings file
        s3 = a.api("/api/destinations/save", {"kind": "s3", "name": "S3", "bucket": "b", "access_key": "AKIA",
                                              "secret_key": "TOPSECRET"})["destination"]
        self.assertTrue(s3["has_secret"])
        self.assertNotIn("TOPSECRET", a.server.settings.path.read_text())
        az = a.api("/api/destinations/save", {"kind": "azure", "name": "Az",
                                              "container_url": "https://x.blob.core.windows.net/c?sv=1&sig=SECRETSIG"})["destination"]
        self.assertEqual(az["container_url"], "https://x.blob.core.windows.net/c")
        self.assertNotIn("SECRETSIG", a.server.settings.path.read_text())
        code, _, body = a.raw("/api/destinations/save", {"kind": "folder", "name": "usb", "path": str(drive)})
        self.assertEqual(code, 400)                          # duplicate name
        a.api("/api/destinations/remove", {"id": s3["id"]})
        self.assertIsNone(a.server.settings.secrets.get(s3["id"]))
        self.assertIn("Copy", a.api("/api/help")["text"])

    def test_schedule_and_settings(self):
        with mock.patch.object(web.schedule, "install") as inst, \
                mock.patch.object(web.schedule, "status", return_value={"enabled": True, "frequency": "daily",
                                                                         "hour": 3, "minute": 15}):
            r = self.a.api("/api/schedule/save", {"frequency": "daily", "hour": 3, "minute": 15, "keep": 7,
                                                  "categories": ["skills", "prompts"]})
            self.assertEqual(r["describe"], "Every day at 03:15")
            args = inst.call_args[0]
            self.assertEqual(args[:4], ("daily", 3, 15, 0))
            self.assertEqual(args[4], ["skills", "prompts"])
            self.assertEqual(args[6], 7)
        s = self.a.api("/api/settings", {"redact": True, "skill_targets": ["upload"]})
        self.assertTrue(s["settings"]["redact"])
        self.assertEqual(s["settings"]["skill_targets"], ["upload"])
        self.assertIn("CLAUDE_HOME", s["roots"])


if __name__ == "__main__":
    unittest.main()
