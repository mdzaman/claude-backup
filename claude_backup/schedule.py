"""Automatic backups through the operating system's own scheduler.

macOS: a LaunchAgent. Windows: a Task Scheduler task. Linux: a crontab line.
The scheduled job runs ``python -m claude_backup backup`` and prunes old
backups so only the newest ``keep`` remain.
"""
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

LABEL = "com.claudebackup.auto"
TASK_NAME = "ClaudeBackupAuto"
CRON_MARK = "# claude-backup-auto"
PKG_PARENT = str(Path(__file__).resolve().parent.parent)
WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _command(categories, out_dir, keep, accounts):
    cmd = [sys.executable, "-m", "claude_backup", "backup", "-o", str(out_dir),
           "--keep", str(keep), "--label", "auto", "--accounts", accounts]
    if categories:
        cmd += ["-c", ",".join(categories)]
    return cmd


def _plist_path():
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def log_path(out_dir):
    return Path(out_dir) / "auto-backup.log"


def install(frequency, hour, minute, weekday, categories, out_dir, keep, accounts="current"):
    """frequency: 'daily' | 'weekly'. weekday: 0=Sunday (weekly only)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _command(categories, out_dir, keep, accounts)
    system = platform.system()
    if system == "Darwin":
        interval = {"Hour": int(hour), "Minute": int(minute)}
        if frequency == "weekly":
            interval["Weekday"] = int(weekday)
        plist = {
            "Label": LABEL,
            "ProgramArguments": cmd,
            "WorkingDirectory": PKG_PARENT,
            "EnvironmentVariables": {"PYTHONPATH": PKG_PARENT},
            "StartCalendarInterval": interval,
            "StandardOutPath": str(log_path(out_dir)),
            "StandardErrorPath": str(log_path(out_dir)),
            "RunAtLoad": False,
        }
        p = _plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _launchctl_unload(p)
        with open(p, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "load", "-w", str(p)], capture_output=True)
    elif system == "Windows":
        tr = subprocess.list2cmdline(cmd)
        args = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", 'cmd /c cd /d "%s" && %s' % (PKG_PARENT, tr),
                "/ST", "%02d:%02d" % (int(hour), int(minute))]
        if frequency == "weekly":
            args += ["/SC", "WEEKLY", "/D", WEEKDAYS[int(weekday)][:3].upper()]
        else:
            args += ["/SC", "DAILY"]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr or r.stdout)
    else:
        dow = str(int(weekday)) if frequency == "weekly" else "*"
        line = "%d %d * * %s cd %s && %s >> %s 2>&1 %s" % (
            int(minute), int(hour), dow, shlex.quote(PKG_PARENT),
            " ".join(shlex.quote(c) for c in cmd), shlex.quote(str(log_path(out_dir))), CRON_MARK)
        _set_crontab([l for l in _get_crontab() if CRON_MARK not in l] + [line])
    return status()


def uninstall():
    system = platform.system()
    if system == "Darwin":
        p = _plist_path()
        _launchctl_unload(p)
        if p.exists():
            p.unlink()
    elif system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    else:
        _set_crontab([l for l in _get_crontab() if CRON_MARK not in l])
    return status()


def status():
    """{'enabled': bool, 'frequency', 'hour', 'minute', 'weekday', 'categories', 'out_dir', 'keep'}"""
    system = platform.system()
    cmd = None
    st = {"enabled": False}
    if system == "Darwin":
        p = _plist_path()
        if p.exists():
            try:
                with open(p, "rb") as f:
                    plist = plistlib.load(f)
            except Exception:
                return st
            cmd = plist.get("ProgramArguments", [])
            iv = plist.get("StartCalendarInterval", {})
            st.update(enabled=True, hour=iv.get("Hour", 0), minute=iv.get("Minute", 0),
                      weekday=iv.get("Weekday", 0),
                      frequency="weekly" if "Weekday" in iv else "daily")
    elif system == "Windows":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            st.update(enabled=True, frequency="weekly" if "Weekly" in r.stdout else "daily")
    else:
        for l in _get_crontab():
            if CRON_MARK in l:
                parts = l.split()
                st.update(enabled=True, minute=int(parts[0]), hour=int(parts[1]),
                          frequency="daily" if parts[4] == "*" else "weekly",
                          weekday=0 if parts[4] == "*" else int(parts[4]))
                cmd = shlex.split(l.split(" && ", 1)[1].split(" >> ")[0])
    if cmd:
        def arg(flag):
            return cmd[cmd.index(flag) + 1] if flag in cmd else None
        st["out_dir"] = arg("-o")
        st["keep"] = int(arg("--keep") or 0)
        st["categories"] = (arg("-c") or "").split(",") if arg("-c") else None
        st["accounts"] = arg("--accounts") or "current"
    return st


def describe(st):
    if not st.get("enabled"):
        return "Off"
    when = "%02d:%02d" % (st.get("hour", 0), st.get("minute", 0))
    if st.get("frequency") == "weekly":
        return "Every %s at %s" % (WEEKDAYS[st.get("weekday", 0) % 7], when)
    return "Every day at %s" % when


def _launchctl_unload(p):
    if p.exists():
        subprocess.run(["launchctl", "unload", "-w", str(p)], capture_output=True)


def _get_crontab():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def _set_crontab(lines):
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
