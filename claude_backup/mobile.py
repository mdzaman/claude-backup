"""Phone access: pair an iPhone or Android phone with the app on this computer.

Claude's data lives on the computer, so the phone is a remote control. Access
is off until switched on, stops when the app quits, uses HTTPS with a
self-signed certificate, needs a separate token, and accepts private-network
clients only. Phones get a reduced set of API calls (see ``REMOTE_ALLOW``).
"""
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

from .engine import BackupError, EXT

DEFAULT_PORT = 8743

# What a phone may call. Restores, migrations, undo, settings changes, adding
# destinations and anything that opens a window on the computer stay local.
REMOTE_ALLOW = {
    "/api/heartbeat", "/api/scan", "/api/backup", "/api/job", "/api/job/cancel", "/api/backups",
    "/api/inspect", "/api/contents", "/api/file", "/api/verify", "/api/settings", "/api/help", "/api/activity",
    "/api/destinations", "/api/destinations/test", "/api/destinations/list", "/api/destinations/upload",
}


def sanitize_remote(path, body, library):
    """Reject calls a phone may not make, and drop parameters it may not set."""
    if path not in REMOTE_ALLOW:
        raise PermissionError("Do this on your computer.")
    if path == "/api/settings":
        return {}                     # read-only from a phone
    body = dict(body)
    for key in ("out", "export", "keep", "label", "dest", "src"):
        body.pop(key, None)
    if "path" in body:
        p = Path(body["path"]).resolve()
        if p.parent != Path(library).resolve() or p.suffix != EXT:
            raise PermissionError("Only backups in your backup folder can be used from a phone.")
    return body


def is_private_client(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private and not ip.is_unspecified


def lan_ip():
    """This computer's address on the local network, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))      # no packets are sent for UDP connect
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


class Pairing:
    """The token, port and certificate kept in ~/.claude-backup/mobile/."""

    def __init__(self, base_dir):
        self.dir = Path(base_dir) / "mobile"
        self.file = self.dir / "pairing.json"
        self.cert = self.dir / "cert.pem"
        self.key = self.dir / "key.pem"

    def _load(self):
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.dir), 0o700)
        fd = os.open(str(self.file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def token(self):
        data = self._load()
        if not data.get("token"):
            data["token"] = secrets.token_urlsafe(24)
            self._save(data)
        return data["token"]

    def rotate(self):
        data = self._load()
        data["token"] = secrets.token_urlsafe(24)
        self._save(data)
        return data["token"]

    def ensure_certificate(self, ip):
        if self.cert.exists() and self.key.exists():
            return
        exe = shutil.which("openssl") or ("/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else None)
        if not exe:
            raise BackupError("Phone access needs OpenSSL to create its secure connection, and it wasn't found.")
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self.dir), 0o700)
        try:
            r = subprocess.run([exe, "req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
                                "-keyout", str(self.key), "-out", str(self.cert), "-subj", "/CN=Claude Backup",
                                "-addext", "subjectAltName=IP:%s" % ip],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise BackupError("Creating the secure connection timed out.")
        if r.returncode:
            raise BackupError("Couldn't create the secure connection: " + r.stderr.strip()[-200:])
        os.chmod(str(self.key), 0o600)

    def ssl_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.cert), str(self.key))
        return ctx


# --------------------------------------------------------------------------
# QR code (version 5, error correction L, byte mode: up to 106 bytes)
# --------------------------------------------------------------------------
QR_SIZE = 37
QR_DATA = 108
QR_ECC = 26

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x = (_x << 1) ^ (0x11D if _x & 0x80 else 0)
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _ecc(data):
    gen = [1]
    for i in range(QR_ECC):
        next_gen = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            next_gen[j] ^= c
            if c:
                next_gen[j + 1] ^= _EXP[_LOG[c] + i]
        gen = next_gen
    rem = list(data) + [0] * QR_ECC
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j, c in enumerate(gen):
                if c:
                    rem[i + j] ^= _EXP[_LOG[c] + _LOG[factor]]
    return rem[len(data):]


def _codewords(text):
    raw = text.encode("utf-8")
    if len(raw) > QR_DATA - 2:
        raise ValueError("Text is too long for this QR code")
    bits = "0100" + format(len(raw), "08b") + "".join(format(b, "08b") for b in raw)
    bits += "0" * min(4, QR_DATA * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < QR_DATA:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    return data + _ecc(data)


def _base():
    n = QR_SIZE
    m = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(x, y, dark):
        if 0 <= x < n and 0 <= y < n:
            m[y][x] = dark
            fixed[y][x] = True

    for i in range(n):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                put(cx + dx, cy + dy, d != 2 and d != 4)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            put(30 + dx, 30 + dy, max(abs(dx), abs(dy)) != 1)
    for i in range(9):                        # reserve format areas
        put(8, i, False)
        put(i, 8, False)
    for i in range(8):
        put(n - 1 - i, 8, False)
        put(8, n - 1 - i, False)
    put(8, n - 8, True)
    # Restore the timing bits the reservation above overwrote.
    put(6, 8, True)
    put(8, 6, True)
    return m, fixed


def _format(m, mask):
    n = QR_SIZE
    data = (1 << 3) | mask                    # error correction level L
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    def bit(i):
        return (bits >> i) & 1 == 1

    for i in range(6):
        m[i][8] = bit(i)
    m[7][8] = bit(6)
    m[8][8] = bit(7)
    m[8][7] = bit(8)
    for i in range(9, 15):
        m[8][14 - i] = bit(i)
    for i in range(8):
        m[8][n - 1 - i] = bit(i)
    for i in range(8, 15):
        m[n - 15 + i][8] = bit(i)
    m[n - 8][8] = True


_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _penalty(m):
    n, score = QR_SIZE, 0
    for lines in (m, [[m[y][x] for y in range(n)] for x in range(n)]):
        for line in lines:
            run = 1
            for a, b in zip(line, line[1:]):
                run = run + 1 if a == b else 1
                score += 3 if run == 5 else 1 if run > 5 else 0
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    return score + abs(dark * 20 - n * n * 10) // (n * n) * 10


def qr_matrix(text):
    words = _codewords(text)
    bits = [(w >> (7 - i)) & 1 == 1 for w in words for i in range(8)]
    base, fixed = _base()
    n, i, slots = QR_SIZE, 0, []
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = (right + 1) & 2 == 0
        for vert in range(n):
            y = n - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not fixed[y][x]:
                    slots.append((x, y, bits[i] if i < len(bits) else False))
                    i += 1
        right -= 2
    best, best_score = None, None
    for mask, fn in enumerate(_MASKS):
        m = [row[:] for row in base]
        for x, y, b in slots:
            m[y][x] = b != fn(x, y)
        _format(m, mask)
        score = _penalty(m)
        if best is None or score < best_score:
            best, best_score = m, score
    return best


def qr_svg(text):
    m = qr_matrix(text)
    path = "".join("M%d %dh1v1h-1z" % (x + 4, y + 4) for y, row in enumerate(m) for x, d in enumerate(row) if d)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges" role="img" '
            'aria-label="Pairing QR code"><rect width="100%%" height="100%%" fill="#fff"/><path fill="#000" d="%s"/></svg>'
            % (QR_SIZE + 8, QR_SIZE + 8, path))
