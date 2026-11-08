"""Drives the desktop app's screens end to end against fake Claude installs."""
import functools
import json
import os
import subprocess
import sys
import plistlib
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from claude_backup import engine, schedule
from claude_backup.env import Env

from test_engine import ACCT_A, ACCT_B, ORG_A, ORG_B, make_export, make_home, write

try:
    import tkinter  # noqa: F401
    HAVE_TK = (sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY"))) and not os.environ.get("CI")
except ImportError:
    HAVE_TK = False


TESTS_DIR = Path(__file__).resolve().parent
CHILD = os.environ.get("CB_GUI_CHILD") == "1"


def isolated(fn):
    """Run the test in a fresh process: Tk on macOS can hang when a second
    root window is created after an earlier one was destroyed. The real app
    only ever has one root window."""
    @functools.wraps(fn)
    def wrapper(self):
        if CHILD:
            return fn(self)
        env = dict(os.environ, CB_GUI_CHILD="1",
                   PYTHONPATH=os.pathsep.join([str(TESTS_DIR.parent), str(TESTS_DIR)]))
        r = subprocess.run([sys.executable, "-m", "unittest", "test_gui.GuiTest." + fn.__name__],
                           cwd=str(TESTS_DIR), env=env, capture_output=True, text=True, timeout=180)
        if r.returncode:
            self.fail(r.stderr[-4000:])
    return wrapper


@unittest.skipUnless(HAVE_TK, "needs a display and tkinter")
class GuiTest(unittest.TestCase):
    def setUp(self):
        self.apps = []
        if not CHILD:
            return
        from claude_backup import gui
        self.gui = gui
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home_a, self.desk_a = make_home(base / "a", ACCT_A, ORG_A, "a@example.com")
        self.home_b, self.desk_b = make_home(base / "b", ACCT_B, ORG_B, "b@example.com", with_data=False)
        self.dialogs = []
        patches = [
            mock.patch.object(gui.messagebox, "askyesno", side_effect=self._yes),
            mock.patch.object(gui.messagebox, "showinfo", side_effect=self._record),
            mock.patch.object(gui.messagebox, "showwarning", side_effect=self._record),
            mock.patch.object(gui.messagebox, "showerror", side_effect=self._error),
            mock.patch.object(gui, "reveal", lambda p: None),
            mock.patch.object(gui.Env, "desktop_running", lambda self: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for app in self.apps:
            app.destroy()
        if CHILD:
            self.tmp.cleanup()

    def _yes(self, *a, **k):
        self.dialogs.append(("ask",) + a)
        return True

    def _record(self, *a, **k):
        self.dialogs.append(("info",) + a)

    def _error(self, *a, **k):
        raise AssertionError("error dialog: %s" % (a,))

    def app(self, home, desk):
        app = self.gui.App(Env(home=home, desktop=desk))
        app.withdraw()
        self.apps.append(app)
        self.pump(app, lambda: app.scan is not None)
        return app

    def pump(self, app, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            if cond():
                return
            time.sleep(0.02)
        self.fail("timed out; status: %s" % app.status_text.cget("text"))

    def wait_task(self, app):
        self.pump(app, lambda: not app.busy)

    @isolated
    def test_full_flow(self):
        a = self.app(self.home_a, self.desk_a)
        dash = a.pages["dashboard"]
        self.assertIn("a@example.com", dash.who.cget("text"))

        # Back up: untick one item, back up the rest
        a.show("backup")
        bp = a.pages["backup"]
        self.assertIn("skill:my-skill", bp.tree.selected())
        bp.tree.toggle("history")  # untick the whole history category
        self.assertFalse(any(i.startswith("history:") for i in bp.tree.selected()))
        bp.go()
        self.wait_task(a)
        backups = engine.list_backups(a.settings.out_dir)
        self.assertEqual(len(backups), 1)
        self.assertNotIn("history", backups[0]["categories"])
        path = backups[0]["path"]

        # Library: list, select, preview a file, verify, extract
        a.show("library")
        lib = a.pages["library"]
        self.assertEqual(list(lib.table.tree.get_children()), [path])
        lib.table.tree.selection_set(path)
        a.update()
        fid = next(k for k, v in lib.files.items() if v == ("skill:my-skill", "SKILL.md"))
        lib.contents.selection_set(fid)
        a.update()
        self.assertIn("Do it.", lib.text.get("1.0", "end"))
        lib.verify()
        self.wait_task(a)
        self.assertIn("All files match", self.dialogs[-1][2])
        lib.contents.selection_set("item:skill:my-skill")
        dest = Path(self.tmp.name) / "extracted"
        with mock.patch.object(self.gui.filedialog, "askdirectory", return_value=str(dest)):
            lib.extract()
        self.assertTrue((dest / "skill__my-skill" / "scripts" / "run.py").exists())

        # Restore on "machine B", signed in to account B
        b = self.app(self.home_b, self.desk_b)
        b.show("restore")
        rp = b.pages["restore"]
        rp.load(path)
        self.assertIn("different account", rp.info.winfo_children()[0].cget("text"))
        self.assertEqual(rp.target_pair(), (ACCT_B, ORG_B))
        rp.go(dry=True)
        self.wait_task(b)
        self.assertFalse((self.home_b / ".claude" / "CLAUDE.md").exists())
        rp.go(dry=False)
        self.wait_task(b)
        self.assertEqual((self.home_b / ".claude" / "CLAUDE.md").read_text(), "Be terse.")
        self.assertTrue((self.home_b / ".claude" / "skills" / "my-skill" / "SKILL.md").exists())
        finish = [w for w in b.winfo_children() if isinstance(w, self.gui.FinishDialog)][-1]
        self.assertIn("skills-to-upload", "".join(finish.tree.checked))
        zip_id = next(iter(finish.tree.checked))
        finish.tree.toggle(zip_id)
        self.assertEqual(b.settings["uploaded"][str(finish.out)], [zip_id])

        # Undo it from the Undo screen
        b.show("undo")
        up = b.pages["undo"]
        snaps = up.table.tree.get_children()
        self.assertEqual(len(snaps), 1)
        up.table.tree.selection_set(snaps[0])
        up.undo()
        self.assertFalse((self.home_b / ".claude" / "CLAUDE.md").exists())
        self.assertIn("Undid restore", up.log.get("1.0", "end"))

    @isolated
    def test_migrate_wizard_new_account(self):
        a = self.app(self.home_a, self.desk_a)
        a.show("migrate")
        mp = a.pages["migrate"]
        exp = Path(self.tmp.name) / "export.zip"
        make_export(exp)
        mp.next()                              # Choose -> Cloud data
        mp.export_field.set(str(exp))
        mp.next()                              # -> What to move
        mp.cat_vars["history"].set(False)
        mp.next()                              # -> Back up
        mp.next()                              # refused: no backup yet
        self.assertEqual(mp.flow()[mp.step][0], "Back up")
        mp._do_backup()
        self.wait_task(a)
        self.assertTrue(Path(mp.backup_path).exists())
        mp.next()                              # -> Sign in
        # The user signs in to account B in Claude: simulate by switching ~/.claude.json
        cj = json.loads((self.home_a / ".claude.json").read_text())
        cj["oauthAccount"] = {"accountUuid": ACCT_B, "organizationUuid": ORG_B, "emailAddress": "b@example.com"}
        write(self.home_a / ".claude.json", json.dumps(cj))
        a.scan = None
        a.refresh_scan()
        self.pump(a, lambda: a.scan is not None)
        mp.next()                              # -> Restore
        self.assertEqual(mp.flow()[mp.step][0], "Restore")
        mp._restore(False)
        self.wait_task(a)
        sdir = Env(home=self.home_a, desktop=self.desk_a).skills_dir(ACCT_B, ORG_B)
        self.assertTrue((sdir / "skills" / "my-skill" / "SKILL.md").exists())
        self.assertTrue(list(Path(mp.result["out_dir"]).glob("claude-ai/artifacts/*/Landing-Page.html")))
        mp.next()                              # -> Upload
        self.assertEqual(mp.flow()[mp.step][0], "Upload")

    @isolated
    def test_migrate_wizard_local(self):
        write(self.desk_a / "local-agent-mode-sessions" / ACCT_B / ORG_B / "scheduled-tasks.json", "{}")
        a = self.app(self.home_a, self.desk_a)
        mp = a.pages["migrate"]
        a.show("migrate")
        mp.mode.set("local")
        mp.render()
        mp.next()                              # -> Accounts
        accts = a.scan["accounts"]
        mp.from_box.current(next(i for i, x in enumerate(accts) if x["account"] == ACCT_A))
        mp.to_box.current(next(i for i, x in enumerate(accts) if x["account"] == ACCT_B))
        mp.next()                              # -> What
        mp.next()                              # -> Cloud data
        mp.next()                              # -> Migrate
        mp._local(False)
        self.wait_task(a)
        self.assertIn("backup", mp.result)
        sdir = Env(home=self.home_a, desktop=self.desk_a).skills_dir(ACCT_B, ORG_B)
        self.assertTrue((sdir / "skills" / "my-skill" / "SKILL.md").exists())

    @isolated
    def test_artifacts_page(self):
        a = self.app(self.home_a, self.desk_a)
        exp = Path(self.tmp.name) / "export.zip"
        make_export(exp)
        a.show("artifacts")
        ap = a.pages["artifacts"]
        ap.zip.set(str(exp))
        ap.load()
        self.wait_task(a)
        self.assertEqual(len(ap.rows), 3)
        ap.q.set("calc")
        a.update()
        self.assertEqual(len(ap.table.tree.get_children()), 1)
        dest = Path(self.tmp.name) / "arts"
        with mock.patch.object(self.gui.filedialog, "askdirectory", return_value=str(dest)):
            ap.save(False)
        self.assertEqual(len(list(dest.rglob("*.*"))), 3)

    @isolated
    def test_schedule_page_writes_launch_agent(self):
        plist = Path(self.tmp.name) / "agent.plist"
        a = self.app(self.home_a, self.desk_a)
        with mock.patch.object(schedule, "_plist_path", return_value=plist), \
                mock.patch.object(schedule.platform, "system", return_value="Darwin"), \
                mock.patch.object(schedule.subprocess, "run"):
            a.show("schedule")
            sp = a.pages["schedule"]
            sp.freq.set("weekly")
            sp.day.current(3)
            sp.hour.set("04")
            sp.minute.set("30")
            sp.keep.set("5")
            sp.cat_vars["history"].set(False)
            sp.save()
            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data["StartCalendarInterval"], {"Hour": 4, "Minute": 30, "Weekday": 3})
            self.assertIn("--keep", data["ProgramArguments"])
            self.assertEqual(schedule.describe(schedule.status()), "Every Wednesday at 04:30")
            self.assertNotIn("history", schedule.status()["categories"])
            sp.off()
            self.assertFalse(plist.exists())

    @isolated
    def test_keep_prunes_old_backups(self):
        env = Env(home=self.home_a, desktop=self.desk_a)
        out = Path(self.tmp.name) / "out"
        with mock.patch.object(engine, "delete_backup", side_effect=lambda p: Path(p).unlink()):
            for i in range(3):
                with mock.patch.object(engine, "now_stamp", return_value="2026010%d-000000" % i):
                    engine.backup(env, ["prompts"], out_dir=out, keep=2)
        names = sorted(p.name for p in out.glob("*" + engine.EXT))
        self.assertEqual(len(names), 2)
        self.assertNotIn("20260100", "".join(names))


if __name__ == "__main__":
    unittest.main()
