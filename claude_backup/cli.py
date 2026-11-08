"""Command line interface. Run with no arguments to open the app window."""
import argparse
import json
import os
import sys

from . import __version__
from . import engine
from .env import Env
from .settings import Settings


def _cats(value):
    if not value or value == "full":
        return None
    cats = [c.strip() for c in value.split(",") if c.strip()]
    bad = set(cats) - set(engine.CATEGORIES)
    if bad:
        raise argparse.ArgumentTypeError("unknown categories: " + ", ".join(sorted(bad)))
    return cats


def _progress(done, total, label):
    if sys.stderr.isatty():
        sys.stderr.write("\r  %d/%d  %-60s" % (done, total, label[:60]))
        if done == total:
            sys.stderr.write("\n")


def _target(args, env):
    if args.to_account:
        if not args.to_org:
            sys.exit("--to-account needs --to-org as well")
        return (args.to_account, args.to_org)
    return None


def _print_restore(res):
    for w in res["warnings"]:
        print("WARNING:", w)
    counts = {}
    for a in res["actions"]:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    t = res["target"]
    print("Target account: %s  org: %s" % (t["account"] or "-", t["org"] or "-"))
    print(("Would perform: " if res["dry_run"] else "Performed: ") +
          ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())))
    if res.get("out_dir"):
        print("Exported files (skill/plugin upload zips, artifacts):", res["out_dir"])
    if res.get("snapshot"):
        print("Undo snapshot:", res["snapshot"])
        print("  undo with: python3 -m claude_backup undo \"%s\"" % res["snapshot"])


def main(argv=None):
    p = argparse.ArgumentParser(prog="claude-backup",
                                description="Back up, restore and migrate Claude Desktop and Claude Code data.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the older Tk desktop window instead")

    s = sub.add_parser("ui", help="open the app in your web browser (default)")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("scan", help="show what can be backed up")
    s.add_argument("--accounts", default="current", choices=["current", "all"])
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("accounts", help="list Claude accounts with data on this machine")

    s = sub.add_parser("backup", help="create a backup")
    s.add_argument("-c", "--categories", type=_cats, default=None,
                   help="comma list of %s, or 'full' (default)" % ",".join(engine.CATEGORIES))
    s.add_argument("--accounts", default="current", choices=["current", "all"])
    s.add_argument("--export", metavar="ZIP", help="include a claude.ai data export (artifacts, projects, prompts)")
    s.add_argument("--redact-secrets", action="store_true", help="blank out API keys/tokens in MCP configs")
    s.add_argument("-o", "--out", help="output folder (default ~/ClaudeBackups)")
    s.add_argument("--keep", type=int, help="afterwards, keep only the newest N backups in the folder")
    s.add_argument("--label", help="word added to the file name")
    s.add_argument("--no-upload", action="store_true",
                   help="don't copy to the destinations marked 'copy every new backup'")
    s.add_argument("--to", action="append", metavar="DEST",
                   help="also copy to this destination (name or id); repeatable")

    s = sub.add_parser("inspect", help="show what a backup contains")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("verify", help="check a backup's checksums")
    s.add_argument("file")

    for name, hlp in (("restore", "restore a backup into the current (or given) account"),):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("file")
        s.add_argument("-c", "--categories", type=_cats, default=None)
        s.add_argument("-i", "--items", help="comma list of item ids (see inspect)")
        s.add_argument("--to-account", help="target account UUID (default: signed-in account)")
        s.add_argument("--to-org", help="target organization UUID")
        s.add_argument("--skills", default="claude-code,upload",
                       help="where restored skills go: claude-code,upload,desktop")
        s.add_argument("--skip-existing", action="store_true", help="never overwrite existing files")
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("-o", "--out", help="folder for upload zips, exports and undo snapshots")
        s.add_argument("--json", action="store_true")

    s = sub.add_parser("migrate", help="copy one local account's data into another local account")
    s.add_argument("--from-account", required=True)
    s.add_argument("--from-org", required=True)
    s.add_argument("--to-account", required=True)
    s.add_argument("--to-org", required=True)
    s.add_argument("-c", "--categories", type=_cats, default=None)
    s.add_argument("--skills", default="claude-code,upload,desktop")
    s.add_argument("--skip-existing", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-o", "--out")

    s = sub.add_parser("undo", help="undo a restore using its snapshot")
    s.add_argument("snapshot")

    s = sub.add_parser("make-app", help="build a double-clickable Claude Backup.app (macOS)")
    s.add_argument("--dest", help="folder to put the app in (default: ./dist)")

    s = sub.add_parser("destinations", help="list drives and clouds that receive copies of backups")
    s.add_argument("--detect", action="store_true", help="also show synced cloud folders and drives found here")

    s = sub.add_parser("upload", help="copy a backup to a destination")
    s.add_argument("file")
    s.add_argument("--to", required=True, metavar="DEST")

    s = sub.add_parser("remote", help="list the backups stored in a destination")
    s.add_argument("dest")

    s = sub.add_parser("download", help="download a backup from a destination into your backup folder")
    s.add_argument("dest")
    s.add_argument("name", help="backup file name (see 'remote')")
    s.add_argument("-o", "--out")

    s = sub.add_parser("list", help="list backups and undo snapshots")
    s.add_argument("-o", "--out")

    args = p.parse_args(argv)
    env = Env()

    try:
        if args.cmd == "gui":
            if sys.platform == "darwin" and not os.environ.get("CLAUDE_BACKUP_NO_REEXEC"):
                # A non-framework Python can't receive clicks on macOS; switch to one that can.
                from .macapp import gui_python, PROJECT
                py = gui_python()
                if py and os.path.realpath(py) != os.path.realpath(sys.executable):
                    os.environ["CLAUDE_BACKUP_NO_REEXEC"] = "1"
                    os.chdir(str(PROJECT))
                    os.execv(py, [py, "-m", "claude_backup", "gui"])
            try:
                from .gui import run
            except ImportError as e:  # no tkinter: fall back to the browser UI
                print("Desktop window unavailable (%s); opening in the browser." % e)
            else:
                run(env)
                return 0
        if args.cmd in (None, "gui", "ui"):
            from .web import serve
            serve(env, port=getattr(args, "port", 0), open_browser=not getattr(args, "no_browser", False))
            return 0

        if args.cmd == "scan":
            r = engine.scan(env, args.accounts)
            if args.json:
                print(json.dumps(r, indent=2))
                return 0
            ca = r["current_account"]
            print("Signed in: %s (account %s, org %s)" % (ca["email"] or "?", ca["account"], ca["org"]))
            for c, s_ in r["categories"].items():
                print("  %-10s %4d items %6d files %10s  %s" % (
                    c, s_["items"], s_["files"], engine.human_size(s_["bytes"]), s_["description"]))
            print("Claude.ai artifacts and conversations are in the cloud: add a claude.ai data "
                  "export with 'backup --export <zip>'.")
            return 0

        if args.cmd == "accounts":
            for a in env.accounts():
                print("%s  account %s  org %s  user skills: %d  %s" % (
                    "*" if a["current"] else " ", a["account"], a["org"], a["user_skills"], a["email"] or ""))
            return 0

        if args.cmd == "backup":
            path, m = engine.backup(env, args.categories, args.accounts, args.export, args.out,
                                    args.redact_secrets, label=args.label, progress=_progress,
                                    keep=args.keep)
            print("Backup written: %s (%d items)" % (path, len(m["items"])))
            from .destinations import copy_to
            st = Settings(env)
            dests = list(st["destinations"])
            if args.no_upload:
                dests = []
            extra = [st.destination(t) for t in (args.to or [])]
            results = copy_to([d for d in dests if d.get("auto")] + [d for d in extra if not d.get("auto") or args.no_upload],
                              path, st.secrets, only_auto=False)
            failed = 0
            for r in results:
                print("  %s %s: %s" % ("OK  " if r["ok"] else "FAIL", r["name"], r["message"]))
                failed += not r["ok"]
            return 3 if failed else 0

        if args.cmd == "inspect":
            r = engine.inspect(args.file)
            if args.json:
                print(json.dumps(r, indent=2))
                return 0
            sa = r["source_account"] or {}
            print("Created %s on %s (%s), account %s" % (r["created_at"], r["host"], r["platform"],
                                                        sa.get("email") or sa.get("account")))
            for it in r["items"]:
                print("  [%-9s] %-48s %4d files %9s  %s" % (it["category"], it["title"][:48], it["files"],
                                                          engine.human_size(it["bytes"]), it["id"]))
            return 0

        if args.cmd == "verify":
            problems = engine.verify(args.file)
            print("\n".join(problems) if problems else "OK: all checksums match")
            return 1 if problems else 0

        if args.cmd == "restore":
            res = engine.restore(
                env, args.file,
                item_ids=[i.strip() for i in args.items.split(",")] if args.items else None,
                categories=args.categories, target=_target(args, env),
                skill_targets=[x.strip() for x in args.skills.split(",") if x.strip()],
                conflict="skip" if args.skip_existing else "overwrite",
                out_dir=args.out, dry_run=args.dry_run, progress=_progress)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_restore(res)
            return 0

        if args.cmd == "migrate":
            res = engine.migrate(env, (args.from_account, args.from_org), (args.to_account, args.to_org),
                                 args.categories, [x for x in args.skills.split(",") if x],
                                 "skip" if args.skip_existing else "overwrite", args.out, args.dry_run)
            print("Source backup:", res["backup"])
            _print_restore(res)
            return 0

        if args.cmd == "undo":
            r = engine.undo(args.snapshot)
            print("Restored %d files, removed %d files created by the restore." % (r["restored"], r["removed"]))
            return 0

        if args.cmd == "make-app":
            from .macapp import build
            app = build(args.dest)
            print("Built %s\nDrag it to your Applications folder, or double-click it here." % app)
            return 0

        if args.cmd in ("destinations", "upload", "remote", "download"):
            from . import destinations as D
            st = Settings(env)
            if args.cmd == "destinations":
                for d in st["destinations"]:
                    where = d.get("path") or d.get("bucket") or d.get("container_url") or d.get("remote") or ""
                    print("%-24s %-7s %-5s keep %-3s %s" % (d["name"], d["type"], "auto" if d.get("auto") else "",
                                                           d.get("keep") or "all", where))
                if not st["destinations"]:
                    print("No destinations yet. Add one in the app (Cloud & drives), or see docs/USER_GUIDE.md.")
                if args.detect:
                    print("\nFound on this computer:")
                    for loc in D.detect_locations(env.home):
                        print("  %-14s %-40s %s" % (loc["kind"], loc["label"], loc["path"]))
                return 0
            dest = D.make(st.destination(args.to if args.cmd == "upload" else args.dest), st.secrets)
            if args.cmd == "upload":
                print("Copied to %s: %s" % (dest.name, dest.upload(args.file)))
            elif args.cmd == "remote":
                for b in dest.list():
                    print("%s  %10s  %s" % (b["modified"][:19], engine.human_size(b["size"]), b["name"]))
            else:
                p = dest.download(args.name, args.out or st.out_dir)
                print("Downloaded:", p)
            return 0

        if args.cmd == "list":
            out = args.out or engine.default_output_dir(env)
            for b in engine.list_backups(out):
                print("%s  %-9s %3d items  %s" % (b["created_at"], engine.human_size(b["size"]), b["items"], b["path"]))
            for s_ in engine.list_snapshots(out):
                print("snapshot  %s" % s_["path"])
            return 0
    except engine.BackupError as e:
        print("Error:", e, file=sys.stderr)
        return 2
    except Exception as e:
        from .destinations import DestinationError
        if isinstance(e, (DestinationError, KeyError)):
            print("Error:", e.args[0] if e.args else e, file=sys.stderr)
            return 2
        raise
    return 0
