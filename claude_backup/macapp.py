"""Build a double-clickable ``Claude Backup.app`` for macOS.

The bundle is a thin launcher: it starts the browser version of this
project's code with the newest Python installed, so the app picks up any
update to the project folder.
"""
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from . import __version__

PROJECT = Path(__file__).resolve().parent.parent
APP_NAME = "Claude Backup"


def _png(width, height, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _inside_round_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def icon_png(size=512):
    """An orange rounded square with a white archive box, drawn with 4x supersampling."""
    s = size / 512.0
    bg = (194, 65, 12)
    shapes = [  # (x0, y0, x1, y1, radius, colour) in 512-space, painted in order
        (24, 24, 488, 488, 110, bg),
        (110, 150, 402, 214, 18, (255, 255, 255)),     # lid
        (130, 224, 382, 392, 18, (255, 255, 255)),     # box
        (212, 262, 300, 290, 14, bg),                  # handle slot
    ]
    rows = []
    n = 4
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(n):
                for sx in range(n):
                    x = (px + (sx + 0.5) / n) / s
                    y = (py + (sy + 0.5) / n) / s
                    col = None
                    for x0, y0, x1, y1, r, c in shapes:
                        if _inside_round_rect(x, y, x0, y0, x1, y1, r):
                            col = c
                    if col:
                        acc[0] += col[0]
                        acc[1] += col[1]
                        acc[2] += col[2]
                        acc[3] += 255
            k = n * n
            a = acc[3] // k
            if a:
                row += [acc[0] * 255 // acc[3], acc[1] * 255 // acc[3], acc[2] * 255 // acc[3], a]
            else:
                row += [0, 0, 0, 0]
        rows.append(row)
    return _png(size, size, rows)


FRAMEWORK_PYTHONS = sorted(Path("/Library/Frameworks/Python.framework/Versions").glob("3.*/bin/python3"),
                           key=lambda p: [int(x) for x in p.parent.parent.name.split(".")], reverse=True) \
    if Path("/Library/Frameworks/Python.framework").is_dir() else []


def _gui_ok(python):
    """True when this Python is a macOS framework build with Tk 8.6.

    Non-framework builds (Anaconda, pyenv, Homebrew's bare binary) aren't
    registered as foreground apps, so their windows don't receive clicks.
    """
    try:
        r = subprocess.run([str(python), "-c", "import sysconfig, tkinter; "
                            "print(bool(sysconfig.get_config_var('PYTHONFRAMEWORK')), tkinter.TkVersion)"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.split() == ["True", "8.6"]


def gui_python():
    """The best Python for showing the window on macOS, or None."""
    for candidate in [Path(sys.executable)] + FRAMEWORK_PYTHONS:
        if candidate.exists() and _gui_ok(candidate):
            return str(candidate)
    return None


def latest_python():
    """The newest Python 3 installed on this Mac (stdlib only is needed)."""
    import glob
    import re
    found = {}
    patterns = ["/opt/homebrew/bin/python3.*", "/usr/local/bin/python3.*",
                "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3",
                str(Path.home() / ".pyenv/versions/3.*/bin/python3")]
    for pat in patterns:
        for p in glob.glob(pat):
            if not os.access(p, os.X_OK) or p.endswith("-config"):
                continue
            try:
                out = subprocess.run([p, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                                     capture_output=True, text=True, timeout=15).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                continue
            if re.fullmatch(r"3\.\d+\.\d+", out):
                found.setdefault(tuple(int(x) for x in out.split(".")), p)
    if not found:
        return sys.executable
    return found[max(found)]


def build(dest=None, python=None):
    dest = Path(dest or PROJECT / "dist")
    python = python or latest_python()
    app = dest / (APP_NAME + ".app")
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    launcher = macos / APP_NAME
    launcher.write_text(
        "#!/bin/bash\n"
        "# Launches Claude Backup from its project folder.\n"
        "PROJECT=%s\n"
        "PY=%s\n"
        "if [ ! -d \"$PROJECT/claude_backup\" ]; then\n"
        "  osascript -e 'display alert \"Claude Backup\" message \"The project folder was moved. "
        "Rebuild the app with: python3 -m claude_backup make-app\"'\n"
        "  exit 1\n"
        "fi\n"
        "mkdir -p \"$HOME/.claude-backup\"\n"
        "# Start the local server in the background (or reopen the page if it's already\n"
        "# running) and exit, so the app doesn't sit in the Dock.\n"
        "cd \"$PROJECT\" && nohup \"$PY\" -m claude_backup ui >> \"$HOME/.claude-backup/app.log\" 2>&1 &\n"
        % (_sh(str(PROJECT)), _sh(python)))
    launcher.chmod(0o755)
    (Path.home() / ".claude-backup").mkdir(exist_ok=True)

    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "com.claudebackup.app",
            "CFBundleVersion": __version__,
            "CFBundleShortVersionString": __version__,
            "CFBundleExecutable": APP_NAME,
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "10.13",
            "NSHighResolutionCapable": True,
        }, f)

    _make_icns(res / "AppIcon.icns")
    subprocess.run(["touch", str(app)])
    return app


def _sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _make_icns(target):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base.png"
        base.write_bytes(icon_png(512))
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                out = iconset / ("icon_%dx%d%s.png" % (px, px, "@2x" if scale == 2 else ""))
                subprocess.run(["sips", "-z", str(px * scale), str(px * scale), str(base), "--out", str(out)],
                               capture_output=True)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], capture_output=True)
        if r.returncode:
            shutil.copy(base, target.with_suffix(".png"))


if __name__ == "__main__":
    print(build(*sys.argv[1:2]))
