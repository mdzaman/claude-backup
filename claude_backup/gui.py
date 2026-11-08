"""Claude Backup desktop app (Tkinter, standard library only).

Screens: Dashboard, Back up, Restore, Migrate (guided wizard), Library,
Artifacts, Schedule, Undo & activity, Settings. Long jobs run on a worker
thread and report progress to the status bar, where they can be cancelled.
"""
import datetime as _dt
import os
import platform
import queue
import subprocess
import tempfile
import threading
import traceback
import webbrowser
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

from . import __version__, engine, schedule
from .claude_ai_export import read_export
from .env import Env
from .settings import Settings

CLAUDE_EXPORT_URL = "https://claude.ai/settings/data-privacy-controls"
CLAUDE_SKILLS_URL = "https://claude.ai/settings/capabilities"
CAT_LABELS = {"skills": "Skills", "plugins": "Plugins", "prompts": "Prompts",
              "artifacts": "Artifacts", "settings": "Settings", "history": "History"}
ON, OFF, MIXED = "☑", "☐", "⊟"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def dark_mode():
    if platform.system() != "Darwin":
        return False
    r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return "Dark" in r.stdout


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
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))  # noqa: only exists on Windows
    else:
        subprocess.run(["xdg-open", str(path)])


def short(uuid):
    return (uuid or "")[:8] + "…" if uuid else "—"


def when(iso):
    try:
        return _dt.datetime.fromisoformat(iso).strftime("%b %d, %Y  %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def acct_label(a):
    bits = []
    if a.get("email"):
        bits.append(a["email"])
    bits.append("account %s · org %s" % (short(a["account"]), short(a["org"])))
    if a.get("user_skills"):
        bits.append("%d skills" % a["user_skills"])
    if a.get("current"):
        bits.append("signed in")
    return "  ·  ".join(bits)


# --------------------------------------------------------------------------
# Reusable widgets
# --------------------------------------------------------------------------
class CheckTree(ttk.Frame):
    """A tree of category rows and item rows, each with a checkbox."""

    def __init__(self, master, columns=(("files", "Files", 60), ("size", "Size", 90)), height=12,
                 on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], height=height, selectmode="browse")
        self.tree.heading("#0", text="Item")
        self.tree.column("#0", width=380, stretch=True)
        for cid, label, width in columns:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, anchor="e", stretch=False)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.checked = {}
        self.labels = {}
        self.tree.bind("<Button-1>", self._click, add=True)
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.focus()))

    def set_groups(self, groups, open_groups=False):
        """groups: [(group_id, label, values, [(item_id, label, values, checked), ...])]"""
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.labels.clear()
        for gid, glabel, gvalues, children in groups:
            self.labels[gid] = glabel
            self.tree.insert("", "end", gid, text=glabel, values=gvalues, open=open_groups)
            for iid, label, values, checked in children:
                self.labels[iid] = label
                self.checked[iid] = bool(checked)
                self.tree.insert(gid, "end", iid, text=label, values=values)
                self._render(iid)
            self._render_group(gid)

    def _render(self, iid):
        self.tree.item(iid, text="%s  %s" % (ON if self.checked[iid] else OFF, self.labels[iid]))

    def _render_group(self, gid):
        kids = self.tree.get_children(gid)
        n = sum(self.checked[k] for k in kids)
        mark = OFF if n == 0 else ON if n == len(kids) else MIXED
        self.tree.item(gid, text="%s  %s  (%d of %d)" % (mark, self.labels[gid], n, len(kids)))

    def _click(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        if "indicator" in str(self.tree.identify_element(event.x, event.y)):
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle(iid)

    def toggle(self, iid):
        if not iid:
            return
        if iid in self.checked:
            self.checked[iid] = not self.checked[iid]
            self._render(iid)
            self._render_group(self.tree.parent(iid))
        else:
            kids = self.tree.get_children(iid)
            value = not all(self.checked[k] for k in kids)
            for k in kids:
                self.checked[k] = value
                self._render(k)
            self._render_group(iid)
        if self.on_change:
            self.on_change()

    def set_all(self, value):
        for k in self.checked:
            self.checked[k] = value
            self._render(k)
        for g in self.tree.get_children(""):
            self._render_group(g)
        if self.on_change:
            self.on_change()

    def selected(self):
        return [k for k, v in self.checked.items() if v]

    def selected_groups(self):
        return [g for g in self.tree.get_children("")
                if any(self.checked[k] for k in self.tree.get_children(g))]


class Table(ttk.Frame):
    """Treeview with headings and a scrollbar."""

    def __init__(self, master, columns, height=8, selectmode="browse"):
        super().__init__(master)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings",
                                 height=height, selectmode=selectmode)
        for cid, label, width, *rest in columns:
            self.tree.heading(cid, text=label)
            anchor = rest[0] if rest else "w"
            self.tree.column(cid, width=width, anchor=anchor, stretch=(anchor == "w"))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def fill(self, rows):
        self.tree.delete(*self.tree.get_children())
        for iid, values in rows:
            self.tree.insert("", "end", iid, values=values)

    def selection(self):
        return list(self.tree.selection())


class PathField(ttk.Frame):
    def __init__(self, master, kind="folder", filetypes=None, placeholder=""):
        super().__init__(master)
        self.var = tk.StringVar()
        self.kind = kind
        self.filetypes = filetypes
        ttk.Entry(self, textvariable=self.var).pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="Choose…", command=self.choose).pack(side="left", padx=(6, 0))
        if placeholder:
            ttk.Label(self, text=placeholder, style="Muted.TLabel").pack(side="left", padx=(8, 0))

    def choose(self):
        if self.kind == "folder":
            p = filedialog.askdirectory(initialdir=self.var.get() or str(Path.home()))
        else:
            p = filedialog.askopenfilename(filetypes=self.filetypes or [("All files", "*")])
        if p:
            self.var.set(p)

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value or "")


def section(master, title, subtitle=None):
    f = ttk.Frame(master)
    ttk.Label(f, text=title, style="Section.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(f, text=subtitle, style="Muted.TLabel", wraplength=760, justify="left").pack(anchor="w", pady=(2, 0))
    return f


def note(master, text, kind="info"):
    style = {"info": "Info.TLabel", "warn": "Warn.TLabel", "ok": "Ok.TLabel", "bad": "Bad.TLabel"}[kind]
    return ttk.Label(master, text=text, style=style, wraplength=760, justify="left", padding=(10, 8))


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------
class PlanDialog(tk.Toplevel):
    """Shows the file operations a restore or migration would perform."""

    def __init__(self, app, result, title="Preview"):
        super().__init__(app)
        self.title(title)
        self.geometry("900x560")
        self.transient(app)
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        t = result["target"]
        ttk.Label(body, text="Into account %s · org %s" % (short(t["account"]), short(t["org"])),
                  style="Section.TLabel").pack(anchor="w")
        counts = {}
        for a in result["actions"]:
            counts[a["action"]] = counts.get(a["action"], 0) + 1
        ttk.Label(body, text="   ".join("%d × %s" % (n, k) for k, n in sorted(counts.items()))
                  or "Nothing to do.", style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        for w in result["warnings"]:
            note(body, w, "warn").pack(fill="x", pady=2)
        table = Table(body, [("action", "Action", 130), ("path", "File", 520), ("note", "Note", 220)], height=16)
        table.pack(fill="both", expand=True, pady=(8, 0))
        table.fill([(str(i), (a["action"], a["path"], a["note"])) for i, a in enumerate(result["actions"])])
        ttk.Button(body, text="Close", command=self.destroy).pack(anchor="e", pady=(10, 0))


class FinishDialog(tk.Toplevel):
    """After a restore: what happened, the upload checklist, and undo."""

    def __init__(self, app, result, title="Restore finished"):
        super().__init__(app)
        self.app = app
        self.result = result
        self.title(title)
        self.geometry("820x600")
        self.transient(app)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        t = result["target"]
        ttk.Label(body, text=title, style="Title.TLabel").pack(anchor="w")
        ok = "Restored into account %s · org %s." % (short(t["account"]), short(t["org"]))
        changed = sum(1 for a in result["actions"] if a["action"] in ("create", "overwrite", "merge into", "register in"))
        note(body, "%s %d files written or merged." % (ok, changed), "ok").pack(fill="x", pady=(8, 4))
        for w in result["warnings"]:
            note(body, w, "warn").pack(fill="x", pady=2)

        out = Path(result["out_dir"]) if result.get("out_dir") else None
        zips = []
        if out:
            zips = sorted(out.glob("skills-to-upload/*.zip")) + sorted(out.glob("plugins-to-upload/*.zip"))
        if zips:
            sec = section(body, "Finish in Claude: upload your skills and plugins",
                          "Skills and plugins are stored in your Claude account in the cloud. Upload each "
                          "file below in the account you restored into. Tick each one off as you go.")
            sec.pack(fill="x", pady=(14, 6))
            steps = ttk.Frame(body)
            steps.pack(fill="x")
            ttk.Button(steps, text="1. Open Claude skills settings",
                       command=lambda: webbrowser.open(CLAUDE_SKILLS_URL)).pack(side="left")
            ttk.Button(steps, text="2. Show the upload files",
                       command=lambda: reveal(out)).pack(side="left", padx=6)
            ttk.Label(steps, text="Skills: Settings → Capabilities → Skills → Upload.   "
                      "Plugins: Customize → Plugins.", style="Muted.TLabel").pack(side="left", padx=6)
            done = set(app.settings["uploaded"].get(str(out), []))
            self.tree = CheckTree(body, columns=(("kind", "Type", 80),), height=10, on_change=self._save)
            groups = []
            for kind in ("skills-to-upload", "plugins-to-upload"):
                files = [z for z in zips if z.parent.name == kind]
                if files:
                    groups.append((kind, "Skills" if kind.startswith("skills") else "Plugins", ("",),
                                   [(str(z), z.stem, ("skill" if kind.startswith("skills") else "plugin",),
                                     str(z) in done) for z in files]))
            self.tree.set_groups(groups, open_groups=True)
            self.tree.pack(fill="both", expand=True, pady=6)
            self.tree.tree.bind("<Double-1>", lambda e: self._reveal_selected())
            self.out = out

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(10, 0))
        if result.get("snapshot"):
            ttk.Button(bar, text="Undo this restore", command=self._undo).pack(side="left")
        if out:
            ttk.Button(bar, text="Open exported files", command=lambda: reveal(out)).pack(side="left", padx=6)
        ttk.Button(bar, text="Done", style="Accent.TButton", command=self.destroy).pack(side="right")

    def _save(self):
        uploaded = dict(self.app.settings["uploaded"])
        uploaded[str(self.out)] = self.tree.selected()
        self.app.settings["uploaded"] = uploaded

    def _reveal_selected(self):
        sel = self.tree.tree.focus()
        if sel and Path(sel).exists():
            reveal(sel)

    def _undo(self):
        if not messagebox.askyesno("Undo restore", "Put back every file this restore changed, and remove the "
                                   "files it added?", parent=self):
            return
        r = engine.undo(self.result["snapshot"])
        self.app.log("Undid restore: %d files put back, %d removed" % (r["restored"], r["removed"]))
        messagebox.showinfo("Undone", "Put back %d files and removed %d." % (r["restored"], r["removed"]), parent=self)
        self.destroy()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
class Page(ttk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, app):
        super().__init__(app.content, padding=(28, 22))
        self.app = app
        ttk.Label(self, text=self.title, style="Title.TLabel").pack(anchor="w")
        if self.subtitle:
            ttk.Label(self, text=self.subtitle, style="Muted.TLabel", wraplength=800,
                      justify="left").pack(anchor="w", pady=(2, 14))
        self.build()

    def build(self):
        pass

    def on_show(self):
        pass

    def on_scan(self):
        """Called whenever the machine scan is refreshed."""


class Dashboard(Page):
    title = "Dashboard"
    subtitle = "Your Claude data on this computer, at a glance."

    def build(self):
        self.who = ttk.Label(self, style="Section.TLabel")
        self.who.pack(anchor="w")
        self.running = ttk.Frame(self)
        self.running.pack(fill="x", pady=(6, 0))
        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x", pady=14)
        self.card_vars = {}
        for i, c in enumerate(engine.CATEGORIES):
            f = ttk.Frame(self.cards, style="Card.TFrame", padding=12)
            f.grid(row=i // 3, column=i % 3, sticky="nsew", padx=(0, 10), pady=(0, 10))
            ttk.Label(f, text=CAT_LABELS[c], style="CardTitle.TLabel").pack(anchor="w")
            v = tk.StringVar(value="…")
            ttk.Label(f, textvariable=v, style="Card.TLabel").pack(anchor="w")
            ttk.Label(f, text=engine.CATEGORIES[c], style="CardMuted.TLabel", wraplength=230,
                      justify="left").pack(anchor="w", pady=(4, 0))
            self.card_vars[c] = v
        for i in range(3):
            self.cards.columnconfigure(i, weight=1, uniform="c")

        status = ttk.Frame(self)
        status.pack(fill="x")
        self.last = ttk.Label(status)
        self.last.pack(anchor="w")
        self.sched = ttk.Label(status)
        self.sched.pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=16)
        ttk.Button(actions, text="Back up everything now", style="Accent.TButton",
                   command=self.quick_backup).pack(side="left")
        ttk.Button(actions, text="Restore…", command=lambda: self.app.show("restore")).pack(side="left", padx=6)
        ttk.Button(actions, text="Move to another account…",
                   command=lambda: self.app.show("migrate")).pack(side="left")
        ttk.Button(actions, text="Refresh", command=self.app.refresh_scan).pack(side="right")

        sec = section(self, "Claude accounts on this computer",
                      "Claude Desktop keeps a separate folder for every account and organization that has signed in.")
        sec.pack(fill="x", pady=(6, 6))
        self.accounts = Table(self, [("email", "Account", 260), ("account", "Account ID", 280),
                                     ("org", "Organization ID", 280), ("skills", "Your skills", 90, "e")], height=5)
        self.accounts.pack(fill="x")
        tip = ttk.Frame(self)
        tip.pack(fill="x", pady=(14, 0))
        note(tip, "Artifacts and chats are stored on claude.ai, not on this computer. To back them up, request "
                  "a data export in Claude (Settings → Privacy → Export data), then add the zip on the "
                  "Artifacts or Back up screen.").pack(side="left", fill="x", expand=True)
        ttk.Button(tip, text="Request export", command=lambda: webbrowser.open(CLAUDE_EXPORT_URL)).pack(side="left", padx=8)

    def on_show(self):
        b = engine.last_backup(self.app.settings.out_dir)
        self.last.configure(text="Last backup:  " + ("%s  ·  %s  ·  %d items" % (
            when(b["created_at"]), engine.human_size(b["size"]), b["items"]) if b else "none yet"))
        self.sched.configure(text="Automatic backups:  " + schedule.describe(schedule.status()))

    def on_scan(self):
        s = self.app.scan
        ca = s["current_account"]
        self.who.configure(text=("Signed in as %s   (account %s · org %s)" % (
            ca["email"] or "unknown", short(ca["account"]), short(ca["org"]))) if ca["account"]
            else "No signed-in Claude account found")
        for w in self.running.winfo_children():
            w.destroy()
        if s["desktop_running"]:
            note(self.running, "Claude Desktop is running. Backing up is fine. Quit it before you restore "
                               "so it doesn't overwrite restored files.", "warn").pack(fill="x")
        for c, st in s["categories"].items():
            self.card_vars[c].set("%d items  ·  %d files  ·  %s" % (
                st["items"], st["files"], engine.human_size(st["bytes"])))
        self.accounts.fill([("%s|%s" % (a["account"], a["org"]),
                             ((a["email"] or "") + ("  (signed in)" if a["current"] else ""),
                              a["account"], a["org"], a["user_skills"])) for a in s["accounts"]])
        self.on_show()

    def quick_backup(self):
        self.app.run_backup(categories=None, item_ids=None)


class BackupPage(Page):
    title = "Back up"
    subtitle = ("Choose what to save. Tick a category to include all of it, or open it to pick single items. "
                "Selecting everything is a full backup.")

    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self.tree.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.tree.set_all(False)).pack(side="left", padx=6)
        self.summary = ttk.Label(bar, style="Muted.TLabel")
        self.summary.pack(side="right")
        self.tree = CheckTree(self, height=11, on_change=self.update_summary)
        self.tree.pack(fill="both", expand=True, pady=8)

        opts = ttk.Frame(self)
        opts.pack(fill="x")
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Accounts").grid(row=0, column=0, sticky="w", pady=4)
        self.accounts = tk.StringVar(value=self.app.settings["accounts"])
        acc = ttk.Frame(opts)
        acc.grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(acc, text="Signed-in account", value="current", variable=self.accounts,
                        command=self.app.refresh_scan_for_accounts).pack(side="left")
        ttk.Radiobutton(acc, text="Every account on this computer", value="all", variable=self.accounts,
                        command=self.app.refresh_scan_for_accounts).pack(side="left", padx=10)
        ttk.Label(opts, text="claude.ai export").grid(row=1, column=0, sticky="w", pady=4)
        self.export = PathField(opts, "file", [("claude.ai export", "*.zip")])
        self.export.grid(row=1, column=1, sticky="ew")
        ttk.Label(opts, text="Save to").grid(row=2, column=0, sticky="w", pady=4)
        self.out = PathField(opts)
        self.out.grid(row=2, column=1, sticky="ew")
        self.redact = tk.BooleanVar(value=self.app.settings["redact"])
        ttk.Checkbutton(opts, text="Remove API keys and tokens from MCP server settings",
                        variable=self.redact).grid(row=3, column=1, sticky="w", pady=4)
        note(self, "Never included: sign-in tokens, cookies, caches and the VM image. Backups aren't "
                   "encrypted, so keep them somewhere safe if they hold MCP API keys.").pack(fill="x", pady=8)
        go = ttk.Frame(self)
        go.pack(fill="x")
        ttk.Button(go, text="Create backup", style="Accent.TButton", command=self.go).pack(side="left")

    def on_show(self):
        if not self.out.get():
            self.out.set(str(self.app.settings.out_dir))

    def on_scan(self):
        s = self.app.scan
        groups = []
        for c in engine.CATEGORIES:
            items = [i for i in s["items"] if i["category"] == c]
            st = s["categories"][c]
            groups.append((c, CAT_LABELS[c], (st["files"], engine.human_size(st["bytes"])),
                           [(i["id"], i["title"] + (("  —  " + i["description"][:70]) if i["description"] else ""),
                             (i["files"], engine.human_size(i["bytes"])), True) for i in items]))
        self.tree.set_groups(groups)
        self.update_summary()

    def update_summary(self):
        sel = set(self.tree.selected())
        items = [i for i in self.app.scan["items"] if i["id"] in sel] if self.app.scan else []
        self.summary.configure(text="%d items · %d files · %s selected" % (
            len(items), sum(i["files"] for i in items), engine.human_size(sum(i["bytes"] for i in items))))

    def go(self):
        sel = self.tree.selected()
        export = self.export.get()
        cats = [c for c in engine.CATEGORIES
                if any(i["category"] == c and i["id"] in sel for i in self.app.scan["items"])]
        if export:
            cats = sorted(set(cats) | {"artifacts", "prompts", "history"})
            sel = sel + ["export:artifacts", "export:prompts", "export:history"]
        if not sel:
            return messagebox.showwarning("Nothing selected", "Choose at least one item to back up.")
        all_ids = {i["id"] for i in self.app.scan["items"]}
        full = all_ids.issubset(set(sel))
        self.app.settings["redact"] = self.redact.get()
        self.app.run_backup(categories=cats, item_ids=None if full else sel, export=export or None,
                            out=self.out.get() or None, redact=self.redact.get(),
                            accounts=self.accounts.get())


class RestorePage(Page):
    title = "Restore"
    subtitle = ("Put a backup back, into this account or a different one. To restore into another account, "
                "sign in to it in Claude Desktop (and Claude Code) first.")

    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.choice = ttk.Combobox(top, state="readonly")
        self.choice.pack(side="left", fill="x", expand=True)
        self.choice.bind("<<ComboboxSelected>>", lambda e: self.load(self.paths[self.choice.current()]))
        ttk.Button(top, text="Open file…", command=self.browse).pack(side="left", padx=6)
        self.info = ttk.Frame(self)
        self.info.pack(fill="x", pady=6)
        self.paths = []
        self.path = None

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self.tree.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.tree.set_all(False)).pack(side="left", padx=6)
        self.tree = CheckTree(self, height=9)
        self.tree.pack(fill="both", expand=True, pady=6)

        opts = ttk.Frame(self)
        opts.pack(fill="x")
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Restore into").grid(row=0, column=0, sticky="w", pady=4)
        tgt = ttk.Frame(opts)
        tgt.grid(row=0, column=1, sticky="ew")
        self.target = ttk.Combobox(tgt, state="readonly")
        self.target.pack(side="left", fill="x", expand=True)
        self.target.bind("<<ComboboxSelected>>", lambda e: self._toggle_custom())
        ttk.Button(tgt, text="Refresh", command=self.app.refresh_scan).pack(side="left", padx=6)
        self.custom = ttk.Frame(opts)
        self.c_acct, self.c_org = tk.StringVar(), tk.StringVar()
        ttk.Label(self.custom, text="Account ID").pack(side="left")
        ttk.Entry(self.custom, textvariable=self.c_acct, width=38).pack(side="left", padx=4)
        ttk.Label(self.custom, text="Org ID").pack(side="left", padx=(8, 0))
        ttk.Entry(self.custom, textvariable=self.c_org, width=38).pack(side="left", padx=4)
        ttk.Label(opts, text="Skills go to").grid(row=2, column=0, sticky="nw", pady=4)
        self.skill_vars = {}
        sk = ttk.Frame(opts)
        sk.grid(row=2, column=1, sticky="w")
        for k, d in engine.SKILL_TARGETS.items():
            v = tk.BooleanVar(value=k in self.app.settings["skill_targets"])
            ttk.Checkbutton(sk, text=d, variable=v).pack(anchor="w")
            self.skill_vars[k] = v
        ttk.Label(opts, text="Existing files").grid(row=3, column=0, sticky="w", pady=4)
        self.conflict = tk.StringVar(value="overwrite")
        cf = ttk.Frame(opts)
        cf.grid(row=3, column=1, sticky="w")
        ttk.Radiobutton(cf, text="Replace (an undo snapshot is saved first)", value="overwrite",
                        variable=self.conflict).pack(side="left")
        ttk.Radiobutton(cf, text="Keep existing", value="skip", variable=self.conflict).pack(side="left", padx=10)
        go = ttk.Frame(self)
        go.pack(fill="x", pady=(10, 0))
        ttk.Button(go, text="Preview changes", command=lambda: self.go(True)).pack(side="left")
        ttk.Button(go, text="Restore", style="Accent.TButton", command=lambda: self.go(False)).pack(side="left", padx=6)

    def on_show(self):
        backups = engine.list_backups(self.app.settings.out_dir)
        self.paths = [b["path"] for b in backups]
        self.choice["values"] = ["%s  ·  %s  ·  %d items  ·  %s" % (
            when(b["created_at"]), ", ".join(b["categories"]), b["items"], engine.human_size(b["size"]))
            for b in backups]
        if self.path and self.path in self.paths:
            self.choice.current(self.paths.index(self.path))
        elif self.paths and not self.path:
            self.choice.current(0)
            self.load(self.paths[0])

    def on_scan(self):
        accts = self.app.scan["accounts"]
        self.targets = [(a["account"], a["org"]) for a in accts]
        self.target["values"] = [acct_label(a) for a in accts] + ["Another account (enter its IDs)…"]
        cur = next((i for i, a in enumerate(accts) if a["current"]), None)
        self.target.current(cur if cur is not None else len(accts))
        self._toggle_custom()

    def _toggle_custom(self):
        if self.target.current() == len(getattr(self, "targets", [])):
            self.custom.grid(row=1, column=1, sticky="w", pady=4)
        else:
            self.custom.grid_forget()

    def browse(self):
        p = filedialog.askopenfilename(filetypes=[("Claude backup", "*" + engine.EXT), ("All files", "*")])
        if p:
            self.load(p)

    def load(self, path):
        try:
            r = engine.inspect(path)
        except engine.BackupError as e:
            return messagebox.showerror("Can't open backup", str(e))
        self.path = path
        if path not in self.paths:
            self.paths.append(path)
            self.choice["values"] = list(self.choice["values"]) + [Path(path).name]
        self.choice.current(self.paths.index(path))
        for w in self.info.winfo_children():
            w.destroy()
        sa = r["source_account"] or {}
        text = "Made %s on %s (%s) from %s." % (when(r["created_at"]), r["host"], r["platform"],
                                               sa.get("email") or short(sa.get("account")))
        if r["redacted"]:
            text += "  API keys were removed from this backup; re-enter them after restoring."
        cur = self.app.scan["current_account"]["account"] if self.app.scan else None
        if cur and sa.get("account") and sa["account"] != cur:
            text += "  It's from a different account: its account IDs will be rewritten to the target's."
        note(self.info, text).pack(fill="x")
        groups = []
        for c in engine.CATEGORIES:
            items = [i for i in r["items"] if i["category"] == c]
            if items:
                groups.append((c, CAT_LABELS[c], (sum(i["files"] for i in items),
                                                  engine.human_size(sum(i["bytes"] for i in items))),
                               [(i["id"], i["title"], (i["files"], engine.human_size(i["bytes"])), True)
                                for i in items]))
        self.tree.set_groups(groups)

    def target_pair(self):
        i = self.target.current()
        if i < len(self.targets):
            return self.targets[i]
        a, o = self.c_acct.get().strip(), self.c_org.get().strip()
        if not (a and o):
            raise engine.BackupError("Enter both the account ID and the organization ID.")
        return (a, o)

    def go(self, dry):
        if not self.path:
            return messagebox.showwarning("No backup", "Choose a backup first.")
        items = self.tree.selected()
        if not items:
            return messagebox.showwarning("Nothing selected", "Choose at least one item to restore.")
        try:
            target = self.target_pair()
        except engine.BackupError as e:
            return messagebox.showwarning("Target account", str(e))
        skills = [k for k, v in self.skill_vars.items() if v.get()]
        self.app.settings["skill_targets"] = skills
        if not dry:
            if self.app.scan and self.app.scan["desktop_running"]:
                if not messagebox.askyesno("Claude Desktop is running",
                                           "Claude Desktop is open and may overwrite restored files. "
                                           "Quit it first for best results.\n\nRestore anyway?"):
                    return
            if not messagebox.askyesno("Restore", "Restore %d items into account %s?\n\nFiles that change are "
                                       "saved to an undo snapshot first." % (len(items), short(target[0]))):
                return
        path, conflict = self.path, self.conflict.get()

        def job(progress):
            return engine.restore(self.app.env, path, item_ids=items, target=target, skill_targets=skills,
                                  conflict=conflict, out_dir=self.app.settings.out_dir,
                                  dry_run=dry, progress=progress)

        def done(res):
            if dry:
                PlanDialog(self.app, res, "Preview: restore")
            else:
                self.app.log("Restored %d items from %s" % (len(items), Path(path).name))
                FinishDialog(self.app, res)
                self.app.refresh_scan()
        self.app.run_task("Planning restore…" if dry else "Restoring…", job, done)


class MigratePage(Page):
    title = "Move to another account"
    subtitle = "A step-by-step guide for moving your skills, prompts, settings and history to a different Claude account."

    def build(self):
        self.steps_bar = ttk.Label(self, style="Muted.TLabel")
        self.steps_bar.pack(anchor="w", pady=(0, 8))
        ttk.Separator(self).pack(fill="x")
        self.body = ttk.Frame(self, padding=(0, 14))
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self)
        nav.pack(fill="x")
        self.back_btn = ttk.Button(nav, text="← Back", command=self.back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next →", style="Accent.TButton", command=self.next)
        self.next_btn.pack(side="right")
        self.restart_btn = ttk.Button(nav, text="Start over", command=self.reset)
        self.restart_btn.pack(side="right", padx=6)
        self.reset()

    # wizard state ----------------------------------------------------------
    def reset(self):
        self.mode = tk.StringVar(value="new")
        self.cat_vars = {c: tk.BooleanVar(value=True) for c in engine.CATEGORIES}
        self.skill_vars = {k: tk.BooleanVar(value=True) for k in engine.SKILL_TARGETS}
        self.export = None
        self.backup_path = None
        self.source = None
        self.result = None
        self.step = 0
        self.render()

    def flow(self):
        if self.mode.get() == "new":
            return [("Choose", self.s_choose), ("Cloud data", self.s_export), ("What to move", self.s_what),
                    ("Back up", self.s_backup), ("Sign in", self.s_signin), ("Restore", self.s_restore),
                    ("Upload", self.s_finish)]
        return [("Choose", self.s_choose), ("Accounts", self.s_accounts), ("What to move", self.s_what),
                ("Cloud data", self.s_export), ("Migrate", self.s_local_run), ("Upload", self.s_finish)]

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        flow = self.flow()
        self.steps_bar.configure(text="   ›   ".join(
            ("● " if i == self.step else "○ ") + name for i, (name, _) in enumerate(flow)))
        flow[self.step][1](self.body)
        self.back_btn.state(["!disabled"] if self.step > 0 else ["disabled"])
        last = self.step == len(flow) - 1
        self.next_btn.configure(text="Finish" if last else "Next →")

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.render()

    def next(self):
        flow = self.flow()
        check = getattr(self, "check_" + flow[self.step][1].__name__[2:], None)
        if check and not check():
            return
        if self.step == len(flow) - 1:
            self.reset()
            self.app.show("dashboard")
            return
        self.step += 1
        self.render()

    # steps ----------------------------------------------------------------
    def s_choose(self, f):
        ttk.Label(f, text="Where is the account you're moving to?", style="Section.TLabel").pack(anchor="w")
        for value, title, desc in (
                ("new", "A new account, or another computer",
                 "Make a backup here, sign in to the new account (on this or another computer), then restore. "
                 "Use this when the new account hasn't signed in to Claude Desktop on this computer yet."),
                ("local", "An account that has already signed in on this computer",
                 "Copy straight from one account's folders to the other's, in one step.")):
            box = ttk.Frame(f, style="Card.TFrame", padding=12)
            box.pack(fill="x", pady=6)
            ttk.Radiobutton(box, text=title, value=value, variable=self.mode, style="Card.TRadiobutton",
                            command=self.render).pack(anchor="w")
            ttk.Label(box, text=desc, style="CardMuted.TLabel", wraplength=720, justify="left").pack(anchor="w", padx=22)

    def s_export(self, f):
        ttk.Label(f, text="Include artifacts and chats? (optional)", style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, text="Artifacts, conversations and project instructions are stored on claude.ai. To bring "
                  "them along, export them from the account you're moving from:", wraplength=760,
                  justify="left").pack(anchor="w", pady=(4, 8))
        for t in ("1.  Open Claude: Settings → Privacy → Export data.",
                  "2.  Wait for the email, then download the .zip.",
                  "3.  Choose the .zip below."):
            ttk.Label(f, text=t).pack(anchor="w")
        ttk.Button(f, text="Open Claude export settings",
                   command=lambda: webbrowser.open(CLAUDE_EXPORT_URL)).pack(anchor="w", pady=10)
        self.export_field = PathField(f, "file", [("claude.ai export", "*.zip")])
        self.export_field.set(self.export)
        self.export_field.pack(fill="x")
        ttk.Label(f, text="Skip this step if you only need local data. You can add the export later.",
                  style="Muted.TLabel").pack(anchor="w", pady=6)

    def check_export(self):
        p = self.export_field.get()
        if p and not zipfile.is_zipfile(p):
            messagebox.showwarning("Not a zip", "That file isn't a claude.ai export .zip.")
            return False
        self.export = p or None
        return True

    def s_what(self, f):
        ttk.Label(f, text="What do you want to move?", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        s = self.app.scan
        for c, d in engine.CATEGORIES.items():
            st = s["categories"][c] if s else None
            text = "%s  —  %s" % (CAT_LABELS[c], d)
            if st:
                text += "   (%d items)" % st["items"]
            ttk.Checkbutton(f, text=text, variable=self.cat_vars[c]).pack(anchor="w", pady=2)
        ttk.Label(f, text="Where should skills go in the new account?", style="Section.TLabel").pack(anchor="w", pady=(14, 6))
        for k, d in engine.SKILL_TARGETS.items():
            ttk.Checkbutton(f, text=d, variable=self.skill_vars[k]).pack(anchor="w", pady=2)

    def check_what(self):
        if not self.cats():
            messagebox.showwarning("Nothing selected", "Choose at least one thing to move.")
            return False
        return True

    def cats(self):
        return [c for c, v in self.cat_vars.items() if v.get()]

    def skills(self):
        return [k for k, v in self.skill_vars.items() if v.get()]

    def s_backup(self, f):
        ttk.Label(f, text="Back up the account you're moving from", style="Section.TLabel").pack(anchor="w")
        ca = self.app.scan["current_account"] if self.app.scan else {}
        ttk.Label(f, text="Signed in now: %s (account %s). This is the account that will be backed up." % (
            ca.get("email") or "unknown", short(ca.get("account"))), wraplength=760).pack(anchor="w", pady=6)
        self.backup_note = ttk.Frame(f)
        self.backup_note.pack(fill="x", pady=6)
        if self.backup_path:
            self._backup_done()
        else:
            ttk.Button(f, text="Create migration backup", style="Accent.TButton",
                       command=self._do_backup).pack(anchor="w", pady=6)

    def _do_backup(self):
        self.source = self.app.scan["current_account"] if self.app.scan else None
        cats, export = self.cats(), self.export

        def job(progress):
            return engine.backup(self.app.env, cats, "current", export, self.app.settings.out_dir,
                                 self.app.settings["redact"], label="migration", progress=progress)

        def done(res):
            self.backup_path = str(res[0])
            self.app.log("Migration backup created: " + Path(self.backup_path).name)
            self.render()
        self.app.run_task("Creating migration backup…", job, done)

    def _backup_done(self):
        note(self.backup_note, "Backup created:\n" + self.backup_path, "ok").pack(fill="x")
        row = ttk.Frame(self.backup_note)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Show in Finder" if platform.system() == "Darwin" else "Show file",
                   command=lambda: reveal(self.backup_path)).pack(side="left")
        ttk.Label(self.backup_note, text="Moving to another computer? Copy this file there, install Claude Backup, "
                  "and open Restore. You can stop here.", style="Muted.TLabel", wraplength=760).pack(anchor="w")

    def check_backup(self):
        if not self.backup_path:
            messagebox.showwarning("Backup first", "Create the migration backup before continuing.")
            return False
        return True

    def s_signin(self, f):
        ttk.Label(f, text="Sign in to the new account", style="Section.TLabel").pack(anchor="w")
        for t in ("1.  In Claude Desktop, open the account menu and sign out, then sign in to the new account.",
                  "2.  If you use Claude Code, run  claude  in a terminal and use  /login  with the new account.",
                  "3.  Quit Claude Desktop (Claude → Quit), so it can't overwrite restored files.",
                  "4.  Click “Check again” below."):
            ttk.Label(f, text=t, wraplength=760, justify="left").pack(anchor="w", pady=2)
        self.signin_status = ttk.Frame(f)
        self.signin_status.pack(fill="x", pady=10)
        ttk.Button(f, text="Check again", command=lambda: self.app.refresh_scan(self._signin_render)).pack(anchor="w")
        self._signin_render()

    def _signin_render(self):
        if not self.signin_status.winfo_exists():
            return
        for w in self.signin_status.winfo_children():
            w.destroy()
        s = self.app.scan
        ca = s["current_account"]
        src = (self.source or {}).get("account")
        if not ca["account"]:
            note(self.signin_status, "No signed-in account found yet.", "warn").pack(fill="x")
        elif src and ca["account"] == src:
            note(self.signin_status, "Still signed in to the original account (%s). Sign in to the new one, then "
                 "check again." % (ca["email"] or short(ca["account"])), "warn").pack(fill="x")
        else:
            note(self.signin_status, "Signed in to the new account: %s (account %s · org %s)." % (
                ca["email"] or "unknown", short(ca["account"]), short(ca["org"])), "ok").pack(fill="x")
        if s["desktop_running"]:
            note(self.signin_status, "Claude Desktop is still running. Quit it before restoring.", "warn").pack(fill="x", pady=4)

    def check_signin(self):
        ca = self.app.scan["current_account"]
        if not ca["account"] or not ca["org"]:
            messagebox.showwarning("Not signed in", "Sign in to the new account, then click Check again.")
            return False
        if self.source and ca["account"] == self.source.get("account"):
            return messagebox.askyesno("Same account", "You're still signed in to the account you backed up. "
                                       "Restore into it anyway?")
        return True

    def s_restore(self, f):
        if not self.backup_path:
            return note(f, "Create the migration backup first (go back one step).", "warn").pack(fill="x")
        ca = self.app.scan["current_account"]
        ttk.Label(f, text="Restore into %s" % (ca["email"] or short(ca["account"])), style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, text="Backup: " + Path(self.backup_path).name, style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Button(row, text="Preview changes", command=lambda: self._restore(True)).pack(side="left")
        ttk.Button(row, text="Restore now", style="Accent.TButton",
                   command=lambda: self._restore(False)).pack(side="left", padx=6)
        if self.result:
            note(f, "Restore finished. Click Next to upload your skills and plugins.", "ok").pack(fill="x", pady=10)

    def _restore(self, dry):
        ca = self.app.scan["current_account"]
        target = (ca["account"], ca["org"])
        path, skills = self.backup_path, self.skills()

        def job(progress):
            return engine.restore(self.app.env, path, target=target, skill_targets=skills,
                                  out_dir=self.app.settings.out_dir, dry_run=dry, progress=progress)

        def done(res):
            if dry:
                return PlanDialog(self.app, res, "Preview: migration")
            self.result = res
            self.app.log("Migrated %s into account %s" % (Path(path).name, short(target[0])))
            self.app.refresh_scan()
            self.render()
        self.app.run_task("Planning…" if dry else "Restoring into the new account…", job, done)

    def check_restore(self):
        if not self.result:
            messagebox.showwarning("Restore first", "Click “Restore now” before continuing.")
            return False
        return True

    def s_accounts(self, f):
        accts = self.app.scan["accounts"] if self.app.scan else []
        labels = [acct_label(a) for a in accts]
        ttk.Label(f, text="Copy from", style="Section.TLabel").pack(anchor="w")
        self.from_box = ttk.Combobox(f, state="readonly", values=labels)
        self.from_box.pack(fill="x", pady=(4, 12))
        ttk.Label(f, text="Copy into", style="Section.TLabel").pack(anchor="w")
        self.to_box = ttk.Combobox(f, state="readonly", values=labels)
        self.to_box.pack(fill="x", pady=4)
        if accts:
            src = max(range(len(accts)), key=lambda i: accts[i]["user_skills"])
            self.from_box.current(src)
            self.to_box.current(next((i for i in range(len(accts)) if i != src), src))
        if getattr(self, "pair", None):
            self.from_box.current(self.pair[0])
            self.to_box.current(self.pair[1])
        ttk.Label(f, text="Don't see the account? It has to sign in to Claude Desktop on this computer at least "
                  "once. Otherwise go back and choose the first option.", style="Muted.TLabel",
                  wraplength=760).pack(anchor="w", pady=10)

    def check_accounts(self):
        i, j = self.from_box.current(), self.to_box.current()
        if i < 0 or j < 0:
            messagebox.showwarning("Choose accounts", "Choose both accounts.")
            return False
        if i == j:
            messagebox.showwarning("Same account", "Choose two different accounts.")
            return False
        self.pair = (i, j)
        return True

    def s_local_run(self, f):
        accts = self.app.scan["accounts"]
        a, b = accts[self.pair[0]], accts[self.pair[1]]
        ttk.Label(f, text="Ready to migrate", style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, text="From:  " + acct_label(a)).pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="Into:   " + acct_label(b)).pack(anchor="w")
        ttk.Label(f, text="Moving: " + ", ".join(CAT_LABELS[c] for c in self.cats()), style="Muted.TLabel").pack(anchor="w", pady=6)
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Preview changes", command=lambda: self._local(True)).pack(side="left")
        ttk.Button(row, text="Migrate now", style="Accent.TButton",
                   command=lambda: self._local(False)).pack(side="left", padx=6)
        if self.result:
            note(f, "Migration finished. Source backup saved to:\n" + self.result.get("backup", ""), "ok").pack(fill="x", pady=10)

    def _local(self, dry):
        accts = self.app.scan["accounts"]
        a, b = accts[self.pair[0]], accts[self.pair[1]]
        cats, skills, export = self.cats(), self.skills(), self.export
        if not dry and self.app.scan["desktop_running"] and not messagebox.askyesno(
                "Claude Desktop is running", "Quit Claude Desktop first so it doesn't overwrite migrated files.\n\n"
                                             "Migrate anyway?"):
            return

        def job(progress):
            return engine.migrate(self.app.env, (a["account"], a["org"]), (b["account"], b["org"]), cats,
                                  skills, "overwrite", self.app.settings.out_dir, dry, export, progress)

        def done(res):
            if dry:
                return PlanDialog(self.app, res, "Preview: migration")
            self.result = res
            self.app.log("Migrated account %s into %s" % (short(a["account"]), short(b["account"])))
            self.app.refresh_scan()
            self.render()
        self.app.run_task("Planning…" if dry else "Migrating…", job, done)

    def check_local_run(self):
        if not self.result:
            messagebox.showwarning("Migrate first", "Click “Migrate now” before continuing.")
            return False
        return True

    def s_finish(self, f):
        ttk.Label(f, text="Last step: upload skills and plugins to the new account", style="Section.TLabel").pack(anchor="w")
        ttk.Label(f, text="Skills and plugins are stored in your Claude account in the cloud. The upload files "
                  "are ready. Open the checklist to upload each one and tick it off.", wraplength=760,
                  justify="left").pack(anchor="w", pady=6)
        ttk.Button(f, text="Open upload checklist", style="Accent.TButton",
                   command=lambda: FinishDialog(self.app, self.result, "Migration finished")).pack(anchor="w", pady=6)
        ttk.Label(f, text="Then restart Claude Desktop. Your Claude Code skills, CLAUDE.md, commands, MCP servers "
                  "and settings are already in place.", style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=10)


class LibraryPage(Page):
    title = "Library"
    subtitle = "Every backup in your backup folder. Pick one to look inside, check it, pull files out, or restore it."

    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.folder = PathField(top)
        self.folder.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Refresh", command=self.on_show).pack(side="left", padx=6)
        ttk.Button(top, text="Add backup file…", command=self.import_file).pack(side="left")
        self.table = Table(self, [("date", "Created", 160), ("cats", "Contents", 300), ("items", "Items", 60, "e"),
                                  ("size", "Size", 80, "e"), ("from", "From", 220)], height=6)
        self.table.pack(fill="x", pady=8)
        self.table.tree.bind("<<TreeviewSelect>>", lambda e: self.select())
        acts = ttk.Frame(self)
        acts.pack(fill="x")
        for text, cmd in (("Restore…", self.restore), ("Verify", self.verify), ("Extract selected…", self.extract),
                          ("Show file", self.show), ("Move to Trash", self.delete)):
            ttk.Button(acts, text=text, command=cmd).pack(side="left", padx=(0, 6))
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(pane)
        self.contents = ttk.Treeview(left, columns=("size",), height=10, selectmode="extended")
        self.contents.heading("#0", text="Contents")
        self.contents.heading("size", text="Size")
        self.contents.column("size", width=80, anchor="e", stretch=False)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.contents.yview)
        self.contents.configure(yscrollcommand=sb.set)
        self.contents.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.contents.bind("<<TreeviewSelect>>", lambda e: self.preview())
        right = ttk.Frame(pane)
        self.text = ScrolledText(right, wrap="word", height=10, font=("Menlo", 11) if platform.system() == "Darwin" else None,
                                 relief="flat", borderwidth=0)
        self.text.pack(fill="both", expand=True)
        pane.add(left, weight=1)
        pane.add(right, weight=1)
        self.files = {}

    def on_show(self):
        if not self.folder.get():
            self.folder.set(str(self.app.settings.out_dir))
        self.backups = engine.list_backups(self.folder.get())
        self.table.fill([(b["path"], (when(b["created_at"]), ", ".join(b["categories"]), b["items"],
                                      engine.human_size(b["size"]), b["email"] or b["host"])) for b in self.backups])

    def current(self):
        sel = self.table.selection()
        if not sel:
            messagebox.showinfo("Choose a backup", "Select a backup in the list first.")
            return None
        return sel[0]

    def select(self):
        sel = self.table.selection()
        if not sel:
            return
        path = sel[0]
        m = engine.read_manifest(path)
        self.contents.delete(*self.contents.get_children())
        self.files.clear()
        for c in engine.CATEGORIES:
            items = [i for i in m["items"] if i["category"] == c]
            if not items:
                continue
            cid = "cat:" + c
            self.contents.insert("", "end", cid, text=CAT_LABELS[c],
                                 values=(engine.human_size(sum(f["size"] for i in items for f in i["files"])),))
            for it in items:
                iid = "item:" + it["id"]
                self.contents.insert(cid, "end", iid, text=it["title"],
                                     values=(engine.human_size(sum(f["size"] for f in it["files"])),))
                for n, f in enumerate(it["files"][:500]):
                    fid = "file:%s:%d" % (it["id"], n)
                    self.files[fid] = (it["id"], f["path"])
                    self.contents.insert(iid, "end", fid, text=f["path"], values=(engine.human_size(f["size"]),))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "Created %s on %s\nAccount: %s\nTool version %s%s\n\nSelect a file on the left to "
                         "preview it." % (when(m["created_at"]), m["host"],
                                          (m.get("source_account") or {}).get("email") or "",
                                          m["tool_version"], "\nAPI keys removed" if m.get("redacted") else ""))

    def preview(self):
        sel = self.contents.selection()
        if not sel or sel[0] not in self.files:
            return
        item_id, rel = self.files[sel[0]]
        text, size = engine.read_file(self.table.selection()[0], item_id, rel)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text if text is not None else "(binary file, %s)" % engine.human_size(size))

    def restore(self):
        p = self.current()
        if p:
            self.app.pages["restore"].path = p
            self.app.show("restore")
            self.app.pages["restore"].load(p)

    def verify(self):
        p = self.current()
        if not p:
            return
        self.app.run_task("Verifying checksums…", lambda progress: engine.verify(p),
                          lambda probs: messagebox.showinfo("Verify", "All files match their checksums." if not probs
                                                            else "Problems found:\n" + "\n".join(probs[:30])))

    def extract(self):
        p = self.current()
        if not p:
            return
        ids = {s[5:] for s in self.contents.selection() if s.startswith("item:")}
        ids |= {self.files[s][0] for s in self.contents.selection() if s in self.files}
        ids |= {i["id"] for s in self.contents.selection() if s.startswith("cat:")
                for i in engine.read_manifest(p)["items"] if i["category"] == s[4:]}
        if not ids:
            return messagebox.showinfo("Extract", "Select items or categories under Contents to extract.")
        dest = filedialog.askdirectory(title="Extract to folder")
        if dest:
            n = engine.extract_items(p, ids, dest)
            self.app.log("Extracted %d files from %s" % (n, Path(p).name))
            reveal(dest)

    def show(self):
        p = self.current()
        if p:
            reveal(p)

    def delete(self):
        p = self.current()
        if p and messagebox.askyesno("Move to Trash", "Move this backup to the Trash?\n\n" + Path(p).name):
            engine.delete_backup(p)
            self.app.log("Moved to Trash: " + Path(p).name)
            self.contents.delete(*self.contents.get_children())
            self.on_show()

    def import_file(self):
        p = filedialog.askopenfilename(filetypes=[("Claude backup", "*" + engine.EXT)])
        if not p:
            return
        try:
            engine.read_manifest(p)
        except engine.BackupError as e:
            return messagebox.showerror("Not a backup", str(e))
        dest = Path(self.folder.get()) / Path(p).name
        if dest.resolve() != Path(p).resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(p, dest)
        self.on_show()


class ArtifactsPage(Page):
    title = "Artifacts"
    subtitle = ("Browse the artifacts in a claude.ai data export. Each artifact is rebuilt at its final version "
                "and can be saved as its own file.")

    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.zip = PathField(top, "file", [("claude.ai export", "*.zip")])
        self.zip.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Load", style="Accent.TButton", command=self.load).pack(side="left", padx=6)
        ttk.Button(top, text="How do I get this?", command=lambda: webbrowser.open(CLAUDE_EXPORT_URL)).pack(side="left")
        filt = ttk.Frame(self)
        filt.pack(fill="x", pady=(10, 0))
        ttk.Label(filt, text="Search").pack(side="left")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *a: self.fill())
        ttk.Entry(filt, textvariable=self.q).pack(side="left", fill="x", expand=True, padx=6)
        self.count = ttk.Label(filt, style="Muted.TLabel")
        self.count.pack(side="left")
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, pady=8)
        self.table = Table(pane, [("title", "Title", 260), ("type", "Type", 160), ("conv", "Conversation", 260),
                                  ("date", "Date", 100), ("ver", "Edits", 50, "e")], height=10, selectmode="extended")
        self.table.tree.bind("<<TreeviewSelect>>", lambda e: self.preview())
        self.table.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.text = ScrolledText(pane, wrap="none", height=10, relief="flat", borderwidth=0,
                                 font=("Menlo", 11) if platform.system() == "Darwin" else None)
        pane.add(self.table, weight=3)
        pane.add(self.text, weight=2)
        acts = ttk.Frame(self)
        acts.pack(fill="x")
        ttk.Button(acts, text="Open", command=self.open_selected).pack(side="left")
        ttk.Button(acts, text="Save selected…", command=lambda: self.save(True)).pack(side="left", padx=6)
        ttk.Button(acts, text="Save all…", command=lambda: self.save(False)).pack(side="left")
        ttk.Button(acts, text="Include in a backup", command=self.to_backup).pack(side="right")
        self.rows, self.data = [], {}

    def load(self):
        p = self.zip.get()
        if not p or not zipfile.is_zipfile(p):
            return messagebox.showwarning("Choose the export", "Choose your claude.ai export .zip first.")

        def job(progress):
            return read_export(p)

        def done(entries):
            import json
            self.data = {rel: data for rel, data, cat in entries if cat == "artifacts"}
            index = json.loads(self.data.pop("index.json", b"[]"))
            self.rows = index
            self.fill()
            prompts = sum(1 for _, _, c in entries if c == "prompts")
            self.app.log("Loaded claude.ai export: %d artifacts, %d prompt files" % (len(index), prompts))
        self.app.run_task("Reading claude.ai export…", job, done)

    def fill(self):
        q = self.q.get().lower()
        rows = [r for r in self.rows if not q or q in (r["title"] + " " + (r["conversation"] or "")).lower()]
        self.table.fill([(r["file"], (r["title"], r["type"] + (" / " + r["language"] if r.get("language") else ""),
                                      r["conversation"] or "", (r["created_at"] or "")[:10], r["versions"]))
                         for r in rows])
        self.count.configure(text="%d of %d" % (len(rows), len(self.rows)))

    def preview(self):
        sel = self.table.selection()
        self.text.delete("1.0", "end")
        if sel:
            self.text.insert("1.0", self.data.get(sel[0], b"").decode("utf-8", "replace"))

    def open_selected(self):
        sel = self.table.selection()
        if not sel:
            return
        d = Path(tempfile.mkdtemp(prefix="claude-artifact-"))
        p = d / Path(sel[0]).name
        p.write_bytes(self.data[sel[0]])
        open_path(p)

    def save(self, selected_only):
        keys = self.table.selection() if selected_only else list(self.data)
        if not keys:
            return messagebox.showinfo("Save", "Select artifacts first.")
        dest = filedialog.askdirectory(title="Save artifacts to")
        if not dest:
            return
        for k in keys:
            p = Path(dest) / k
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.data[k])
        self.app.log("Saved %d artifacts to %s" % (len(keys), dest))
        reveal(dest)

    def to_backup(self):
        self.app.pages["backup"].export.set(self.zip.get())
        self.app.show("backup")


class SchedulePage(Page):
    title = "Automatic backups"
    subtitle = ("Back up on a schedule, even when this app is closed. Older automatic backups are moved to the "
                "Trash so only the newest ones are kept.")

    def build(self):
        self.status = ttk.Label(self, style="Section.TLabel")
        self.status.pack(anchor="w", pady=(0, 10))
        g = ttk.Frame(self)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="How often").grid(row=0, column=0, sticky="w", pady=6)
        self.freq = tk.StringVar(value="daily")
        fr = ttk.Frame(g)
        fr.grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(fr, text="Every day", value="daily", variable=self.freq, command=self._freq).pack(side="left")
        ttk.Radiobutton(fr, text="Every week on", value="weekly", variable=self.freq, command=self._freq).pack(side="left", padx=(12, 4))
        self.day = ttk.Combobox(fr, state="readonly", values=schedule.WEEKDAYS, width=11)
        self.day.current(0)
        self.day.pack(side="left")
        ttk.Label(g, text="At").grid(row=1, column=0, sticky="w", pady=6)
        tm = ttk.Frame(g)
        tm.grid(row=1, column=1, sticky="w")
        self.hour = tk.StringVar(value="02")
        self.minute = tk.StringVar(value="00")
        ttk.Spinbox(tm, from_=0, to=23, width=4, textvariable=self.hour, format="%02.0f").pack(side="left")
        ttk.Label(tm, text=":").pack(side="left")
        ttk.Spinbox(tm, from_=0, to=59, width=4, textvariable=self.minute, format="%02.0f").pack(side="left")
        ttk.Label(tm, text="  (your computer must be awake; missed runs happen at next wake on macOS)",
                  style="Muted.TLabel").pack(side="left")
        ttk.Label(g, text="What").grid(row=2, column=0, sticky="nw", pady=6)
        cats = ttk.Frame(g)
        cats.grid(row=2, column=1, sticky="w")
        self.cat_vars = {c: tk.BooleanVar(value=True) for c in engine.CATEGORIES}
        for i, c in enumerate(engine.CATEGORIES):
            ttk.Checkbutton(cats, text=CAT_LABELS[c], variable=self.cat_vars[c]).grid(row=0, column=i, sticky="w", padx=(0, 10))
        ttk.Label(g, text="Accounts").grid(row=3, column=0, sticky="w", pady=6)
        self.accounts = tk.StringVar(value="current")
        ac = ttk.Frame(g)
        ac.grid(row=3, column=1, sticky="w")
        ttk.Radiobutton(ac, text="Signed-in account", value="current", variable=self.accounts).pack(side="left")
        ttk.Radiobutton(ac, text="Every account", value="all", variable=self.accounts).pack(side="left", padx=10)
        ttk.Label(g, text="Keep newest").grid(row=4, column=0, sticky="w", pady=6)
        kp = ttk.Frame(g)
        kp.grid(row=4, column=1, sticky="w")
        self.keep = tk.StringVar(value=str(self.app.settings["keep"]))
        ttk.Spinbox(kp, from_=1, to=365, width=5, textvariable=self.keep).pack(side="left")
        ttk.Label(kp, text=" backups in the folder (older ones go to the Trash)", style="Muted.TLabel").pack(side="left")
        ttk.Label(g, text="Folder").grid(row=5, column=0, sticky="w", pady=6)
        self.folder = PathField(g)
        self.folder.grid(row=5, column=1, sticky="ew")
        note(self, "Keep-newest applies to every backup in the folder. To protect a backup, keep it in another folder.",
             "info").pack(fill="x", pady=10)
        acts = ttk.Frame(self)
        acts.pack(fill="x")
        ttk.Button(acts, text="Save schedule", style="Accent.TButton", command=self.save).pack(side="left")
        ttk.Button(acts, text="Turn off", command=self.off).pack(side="left", padx=6)
        ttk.Button(acts, text="Run a backup now", command=self.run_now).pack(side="left")
        ttk.Button(acts, text="Open log", command=self.open_log).pack(side="right")

    def _freq(self):
        self.day.state(["!disabled"] if self.freq.get() == "weekly" else ["disabled"])

    def on_show(self):
        st = schedule.status()
        self.status.configure(text="Status:  " + schedule.describe(st))
        if st.get("enabled"):
            self.freq.set(st.get("frequency", "daily"))
            self.day.current(st.get("weekday", 0) % 7)
            self.hour.set("%02d" % st.get("hour", 2))
            self.minute.set("%02d" % st.get("minute", 0))
            if st.get("categories"):
                for c, v in self.cat_vars.items():
                    v.set(c in st["categories"])
            if st.get("keep"):
                self.keep.set(str(st["keep"]))
            self.accounts.set(st.get("accounts", "current"))
        self.folder.set(st.get("out_dir") or str(self.app.settings.out_dir))
        self._freq()

    def _values(self):
        cats = [c for c, v in self.cat_vars.items() if v.get()]
        if not cats:
            raise ValueError("Choose at least one category.")
        h, m, k = int(float(self.hour.get())), int(float(self.minute.get())), int(self.keep.get())
        if not (0 <= h <= 23 and 0 <= m <= 59 and k >= 1):
            raise ValueError("Check the time and the number of backups to keep.")
        return cats, h, m, k

    def save(self):
        try:
            cats, h, m, k = self._values()
            schedule.install(self.freq.get(), h, m, max(self.day.current(), 0),
                             None if len(cats) == len(engine.CATEGORIES) else cats,
                             self.folder.get() or self.app.settings.out_dir, k, self.accounts.get())
        except Exception as e:
            return messagebox.showerror("Couldn't save schedule", str(e))
        self.app.settings["keep"] = k
        self.app.log("Automatic backups: " + schedule.describe(schedule.status()))
        self.on_show()
        messagebox.showinfo("Saved", "Automatic backups are on: " + schedule.describe(schedule.status()) + ".")

    def off(self):
        schedule.uninstall()
        self.app.log("Automatic backups turned off")
        self.on_show()

    def run_now(self):
        try:
            cats, _, _, k = self._values()
        except ValueError as e:
            return messagebox.showwarning("Check settings", str(e))
        self.app.run_backup(categories=cats, item_ids=None, out=self.folder.get() or None,
                            accounts=self.accounts.get(), keep=k, label="auto")

    def open_log(self):
        p = schedule.log_path(self.folder.get() or self.app.settings.out_dir)
        if p.exists():
            open_path(p)
        else:
            messagebox.showinfo("No log yet", "The schedule hasn't run yet.")


class UndoPage(Page):
    title = "Undo & activity"
    subtitle = ("Before every restore, the files it will change are saved here. Undo puts them back and removes "
                "the files the restore added.")

    def build(self):
        self.table = Table(self, [("name", "Snapshot", 320), ("date", "Restore made", 180),
                                  ("files", "Files", 80, "e"), ("size", "Size", 90, "e")], height=7)
        self.table.pack(fill="x")
        acts = ttk.Frame(self)
        acts.pack(fill="x", pady=8)
        ttk.Button(acts, text="Undo this restore", style="Accent.TButton", command=self.undo).pack(side="left")
        ttk.Button(acts, text="What it changed", command=self.details).pack(side="left", padx=6)
        ttk.Button(acts, text="Delete snapshot", command=self.delete).pack(side="left")
        section(self, "Activity").pack(fill="x", pady=(16, 4))
        self.log = ScrolledText(self, height=12, wrap="word", relief="flat", borderwidth=0)
        self.log.pack(fill="both", expand=True)

    def on_show(self):
        import json
        rows = []
        for s in engine.list_snapshots(self.app.settings.out_dir):
            try:
                with zipfile.ZipFile(s["path"]) as z:
                    n = len(json.loads(z.read("snapshot.json"))["entries"])
            except Exception:
                n = "?"
            stamp = s["name"][len("pre-restore-"):-4]
            try:
                stamp = _dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%b %d, %Y  %H:%M")
            except ValueError:
                pass
            rows.append((s["path"], (s["name"], stamp, n, engine.human_size(s["size"]))))
        self.table.fill(rows)
        self.log.delete("1.0", "end")
        self.log.insert("1.0", self.app.read_log())
        self.log.see("end")

    def _sel(self):
        s = self.table.selection()
        if not s:
            messagebox.showinfo("Choose a snapshot", "Select a snapshot first.")
        return s[0] if s else None

    def undo(self):
        p = self._sel()
        if p and messagebox.askyesno("Undo restore", "Put back every file this restore changed, and remove the "
                                     "files it added?"):
            r = engine.undo(p)
            self.app.log("Undid restore %s: %d put back, %d removed" % (Path(p).name, r["restored"], r["removed"]))
            messagebox.showinfo("Undone", "Put back %d files and removed %d." % (r["restored"], r["removed"]))
            self.app.refresh_scan()
            self.on_show()

    def details(self):
        import json
        p = self._sel()
        if not p:
            return
        with zipfile.ZipFile(p) as z:
            entries = json.loads(z.read("snapshot.json"))["entries"]
        win = tk.Toplevel(self)
        win.title("Files changed by this restore")
        win.geometry("820x480")
        t = Table(win, [("what", "Undo will", 120), ("path", "File", 660)], height=18)
        t.pack(fill="both", expand=True, padx=12, pady=12)
        t.fill([(str(i), ("put back" if e["existed"] else "remove", e["path"])) for i, e in enumerate(entries)])

    def delete(self):
        p = self._sel()
        if p and messagebox.askyesno("Delete snapshot", "Delete this snapshot? You won't be able to undo that restore."):
            Path(p).unlink()
            self.on_show()


class SettingsPage(Page):
    title = "Settings"

    def build(self):
        g = ttk.Frame(self)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="Backup folder").grid(row=0, column=0, sticky="w", pady=6)
        self.folder = PathField(g)
        self.folder.grid(row=0, column=1, sticky="ew")
        ttk.Label(g, text="Default skill targets").grid(row=1, column=0, sticky="nw", pady=6)
        sk = ttk.Frame(g)
        sk.grid(row=1, column=1, sticky="w")
        self.skill_vars = {}
        for k, d in engine.SKILL_TARGETS.items():
            v = tk.BooleanVar(value=k in self.app.settings["skill_targets"])
            ttk.Checkbutton(sk, text=d, variable=v).pack(anchor="w")
            self.skill_vars[k] = v
        self.redact = tk.BooleanVar(value=self.app.settings["redact"])
        ttk.Checkbutton(g, text="Remove API keys and tokens from backups by default",
                        variable=self.redact).grid(row=2, column=1, sticky="w", pady=6)
        ttk.Button(self, text="Save settings", style="Accent.TButton", command=self.save).pack(anchor="w", pady=10)

        section(self, "Where Claude keeps your data").pack(fill="x", pady=(16, 4))
        self.roots = Table(self, [("root", "Location", 160), ("path", "Folder", 600)], height=5)
        self.roots.pack(fill="x")
        self.roots.tree.bind("<Double-1>", lambda e: self.roots.selection() and reveal(
            self.roots.tree.item(self.roots.selection()[0])["values"][1]))
        section(self, "About").pack(fill="x", pady=(16, 4))
        ttk.Label(self, text="Claude Backup %s  ·  Python %s  ·  settings in %s" % (
            __version__, platform.python_version(), self.app.settings.path), style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(self)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Open in web browser instead", command=self.web).pack(side="left")

    def on_show(self):
        self.folder.set(str(self.app.settings.out_dir))
        labels = {"CLAUDE_HOME": "Claude Code", "HOME": "Home (.claude.json)", "DESKTOP": "Claude Desktop",
                  "COWORK_FILES": "Cowork files"}
        self.roots.fill([(k, (labels.get(k, k), str(v))) for k, v in self.app.env.roots.items()])

    def save(self):
        self.app.settings["out_dir"] = self.folder.get() or None
        self.app.settings["skill_targets"] = [k for k, v in self.skill_vars.items() if v.get()]
        self.app.settings["redact"] = self.redact.get()
        self.app.log("Settings saved")
        messagebox.showinfo("Saved", "Settings saved.")

    def web(self):
        import sys
        subprocess.Popen([sys.executable, "-m", "claude_backup", "ui"],
                         cwd=str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# Application window
# --------------------------------------------------------------------------
NAV = [("dashboard", "Dashboard", Dashboard), ("backup", "Back up", BackupPage),
       ("restore", "Restore", RestorePage), ("migrate", "Move accounts", MigratePage),
       ("library", "Library", LibraryPage), ("artifacts", "Artifacts", ArtifactsPage),
       ("schedule", "Automatic", SchedulePage), ("undo", "Undo & activity", UndoPage),
       ("settings", "Settings", SettingsPage)]


class App(tk.Tk):
    def __init__(self, env):
        super().__init__()
        self.env = env
        self.settings = Settings(env)
        self.scan = None
        self.busy = False
        self.cancel = threading.Event()
        self.title("Claude Backup")
        self.geometry("1180x800")
        self.minsize(960, 640)
        self.dark = dark_mode()
        self._style()

        self.sidebar = tk.Frame(self, bg=self.c["side"], width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text="Claude Backup", bg=self.c["side"], fg=self.c["ink"],
                 font=self.f_brand, anchor="w").pack(fill="x", padx=18, pady=(20, 2))
        tk.Label(self.sidebar, text="v" + __version__, bg=self.c["side"], fg=self.c["muted"],
                 anchor="w").pack(fill="x", padx=18, pady=(0, 16))
        self.nav = {}
        for key, label, _ in NAV:
            l = tk.Label(self.sidebar, text=label, bg=self.c["side"], fg=self.c["ink"], anchor="w",
                         padx=18, pady=7, cursor="hand2", font=self.f_nav)
            l.pack(fill="x", padx=8)
            l.bind("<Button-1>", lambda e, k=key: self.show(k))
            self.nav[key] = l

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(right)
        self.content.pack(fill="both", expand=True)
        status = ttk.Frame(right, padding=(16, 8), style="Status.TFrame")
        status.pack(fill="x")
        self.status_text = ttk.Label(status, text="Ready", style="Status.TLabel")
        self.status_text.pack(side="left")
        self.cancel_btn = ttk.Button(status, text="Cancel", command=self.cancel.set)
        self.progress = ttk.Progressbar(status, length=260, mode="determinate")

        self.pages = {key: cls(self) for key, _, cls in NAV}
        self.current = None
        self.show("dashboard")
        self.refresh_scan()
        self.after(150, self._bring_to_front)

    def _bring_to_front(self):
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of '
                            'the first process whose unix id is %d to true' % os.getpid()],
                           capture_output=True)

    # look ------------------------------------------------------------------
    def _style(self):
        if self.dark:
            self.c = dict(side="#1f1e1c", sel="#3a2a21", ink="#ecebe6", muted="#a29e93", card="#2a2926",
                          accent="#f08a5d", info="#3a2a21", warn="#3a3318", warnfg="#facc15", ok="#1d3326",
                          okfg="#4ade80", bad="#3d2222", badfg="#f87171")
        else:
            self.c = dict(side="#efede7", sel="#e3ddd0", ink="#1f1e1b", muted="#6b675e", card="#f7f6f2",
                          accent="#c2410c", info="#fbeee6", warn="#fdf6e3", warnfg="#8a5300", ok="#e7f5ec",
                          okfg="#15803d", bad="#fdecec", badfg="#b91c1c")
        base = tkfont.nametofont("TkDefaultFont")
        fam, size = base.actual("family"), base.actual("size")
        self.f_brand = tkfont.Font(family=fam, size=size + 4, weight="bold")
        self.f_nav = tkfont.Font(family=fam, size=size + 1)
        self.f_nav_sel = tkfont.Font(family=fam, size=size + 1, weight="bold")
        s = ttk.Style(self)
        if platform.system() != "Darwin":
            s.theme_use("clam")
        c = self.c
        s.configure("Title.TLabel", font=(fam, size + 10, "bold"))
        s.configure("Section.TLabel", font=(fam, size + 2, "bold"))
        s.configure("Muted.TLabel", foreground=c["muted"])
        s.configure("Card.TFrame", background=c["card"], relief="flat")
        s.configure("CardTitle.TLabel", background=c["card"], font=(fam, size + 1, "bold"))
        s.configure("Card.TLabel", background=c["card"])
        s.configure("CardMuted.TLabel", background=c["card"], foreground=c["muted"])
        s.configure("Card.TRadiobutton", background=c["card"], font=(fam, size + 1, "bold"))
        s.configure("Info.TLabel", background=c["info"])
        s.configure("Warn.TLabel", background=c["warn"], foreground=c["warnfg"])
        s.configure("Ok.TLabel", background=c["ok"], foreground=c["okfg"])
        s.configure("Bad.TLabel", background=c["bad"], foreground=c["badfg"])
        s.configure("Accent.TButton", font=(fam, size, "bold"))
        s.configure("Treeview", rowheight=24)

    # navigation ------------------------------------------------------------
    def show(self, key):
        if self.current:
            self.pages[self.current].pack_forget()
            self.nav[self.current].configure(bg=self.c["side"], font=self.f_nav)
        self.current = key
        self.nav[key].configure(bg=self.c["sel"], font=self.f_nav_sel)
        page = self.pages[key]
        page.pack(fill="both", expand=True)
        try:
            page.on_show()
        except Exception as e:
            traceback.print_exc()
            self.set_status("Error: %s" % e)

    # scanning --------------------------------------------------------------
    def refresh_scan(self, then=None):
        self.set_status("Scanning your Claude data…")
        accounts = self.pages["backup"].accounts.get() if hasattr(self, "pages") else "current"
        q = queue.Queue()

        def work():
            try:
                q.put(("ok", engine.scan(self.env, accounts)))
            except Exception as e:
                q.put(("err", e))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                kind, val = q.get_nowait()
            except queue.Empty:
                return self.after(100, poll)
            if kind == "err":
                self.set_status("Scan failed: %s" % val)
                return
            self.scan = val
            for p in self.pages.values():
                try:
                    p.on_scan()
                except Exception:
                    traceback.print_exc()
            if not self.busy:
                self.set_status("Ready")
            if then:
                then()
        poll()

    def refresh_scan_for_accounts(self):
        self.settings["accounts"] = self.pages["backup"].accounts.get()
        self.refresh_scan()

    # background jobs ---------------------------------------------------------
    def set_status(self, text):
        self.status_text.configure(text=text)

    def run_task(self, title, fn, done=None):
        if self.busy:
            return messagebox.showinfo("Busy", "Please wait for the current task to finish.")
        self.busy = True
        self.cancel.clear()
        self.set_status(title)
        self.progress.configure(value=0, maximum=100)
        self.progress.pack(side="right")
        self.cancel_btn.pack(side="right", padx=8)
        self.configure(cursor="watch")
        q = queue.Queue()

        def progress(n, total, label):
            if self.cancel.is_set():
                raise engine.Cancelled("Cancelled")
            q.put(("p", n, total, label))

        def work():
            try:
                q.put(("done", fn(progress)))
            except Exception as e:
                q.put(("err", e, traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

        def finish():
            self.busy = False
            self.progress.pack_forget()
            self.cancel_btn.pack_forget()
            self.configure(cursor="")

        def poll():
            last = None
            try:
                while True:
                    msg = q.get_nowait()
                    if msg[0] == "p":
                        last = msg
                        continue
                    finish()
                    if msg[0] == "done":
                        self.set_status("Done")
                        if done:
                            done(msg[1])
                    elif isinstance(msg[1], engine.Cancelled):
                        self.set_status("Cancelled")
                    else:
                        self.set_status("Failed")
                        self.log("Error: %s" % msg[1])
                        print(msg[2])
                        messagebox.showerror("Something went wrong", str(msg[1]))
                    return
            except queue.Empty:
                pass
            if last:
                _, n, total, label = last
                self.progress.configure(value=100.0 * n / max(total, 1))
                self.set_status("%s  %d/%d  %s" % (title, n, total, label[:50]))
            self.after(80, poll)
        poll()

    def run_backup(self, categories, item_ids, export=None, out=None, redact=None, accounts=None,
                   keep=None, label=None):
        out = out or str(self.settings.out_dir)
        redact = self.settings["redact"] if redact is None else redact
        accounts = accounts or self.settings["accounts"]

        def job(progress):
            return engine.backup(self.env, categories, accounts, export, out, redact, label=label,
                                 progress=progress, item_ids=item_ids, keep=keep)

        def done(res):
            path, m = res
            self.log("Backup created: %s (%d items, %s)" % (path.name, len(m["items"]),
                                                           engine.human_size(path.stat().st_size)))
            if messagebox.askyesno("Backup created", "Saved %d items (%s) to:\n\n%s\n\nShow it in the Library?" % (
                    len(m["items"]), engine.human_size(path.stat().st_size), path)):
                self.show("library")
                lib = self.pages["library"]
                if str(path) in lib.table.tree.get_children():
                    lib.table.tree.selection_set(str(path))
            elif self.current == "dashboard":
                self.pages["dashboard"].on_show()
        self.run_task("Backing up…", job, done)

    # activity log ------------------------------------------------------------
    @property
    def log_file(self):
        return self.settings.path.parent / "activity.log"

    def log(self, text):
        line = "%s  %s\n" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        self.set_status(text)

    def read_log(self):
        try:
            return "".join(self.log_file.read_text(encoding="utf-8").splitlines(True)[-500:])
        except OSError:
            return "No activity yet.\n"


def run(env=None):
    app = App(env or Env())
    app.mainloop()
