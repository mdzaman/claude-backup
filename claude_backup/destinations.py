"""Places to keep copies of backups: drives, synced cloud folders and cloud storage.

Four kinds, all with the standard library only:

* ``folder``  an external drive, a NAS, or a folder that a cloud app keeps in
              sync (Google Drive, OneDrive, Dropbox, iCloud Drive, Box).
* ``s3``      Amazon S3, or any S3-compatible service (Backblaze B2, Wasabi,
              Cloudflare R2, MinIO, DigitalOcean Spaces...), via SigV4 requests.
* ``azure``   Azure Blob Storage, through a container SAS URL.
* ``rclone``  any of rclone's 70+ providers (Google Drive, OneDrive, Dropbox,
              Box, pCloud, Mega, SFTP...) through the rclone command.

Every destination can upload, list, download and delete backups, and keep
only the newest N. Only files named ``claude-backup-*.claudebackup`` are ever
listed or deleted.
"""
import base64
import configparser
import datetime as _dt
import getpass
import glob
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from .engine import EXT

NAME_RE = re.compile(r"^claude-backup-.*" + re.escape(EXT) + "$")
CHUNK = 1 << 20


class DestinationError(Exception):
    pass


def _noop(*a):
    pass


class Destination:
    type = ""

    def __init__(self, cfg, secrets=None):
        self.cfg = cfg
        self.secrets = secrets

    @property
    def name(self):
        return self.cfg.get("name") or self.type

    def test(self):
        self.list()
        return "Connected."

    def prune(self, keep):
        if not keep:
            return []
        items = sorted(self.list(), key=lambda x: x["name"], reverse=True)
        removed = []
        for it in items[int(keep):]:
            self.delete(it["name"])
            removed.append(it["name"])
        return removed

    @staticmethod
    def _check_name(name):
        if not NAME_RE.match(name) or "/" in name or "\\" in name:
            raise DestinationError("Not a Claude backup file name: %s" % name)


# --------------------------------------------------------------------------
# Folder: external drive, NAS, synced cloud folder
# --------------------------------------------------------------------------
class FolderDest(Destination):
    type = "folder"

    @property
    def base(self):
        root = Path(os.path.expanduser(self.cfg.get("path", "")))
        sub = self.cfg.get("subfolder", "Claude Backups")
        return root / sub if sub else root

    def _ready(self):
        root = Path(os.path.expanduser(self.cfg.get("path", "")))
        if not self.cfg.get("path") or not root.is_dir():
            raise DestinationError("%s isn't available. Is the drive connected, or the cloud app running?"
                                   % (self.cfg.get("path") or "The folder"))
        self.base.mkdir(parents=True, exist_ok=True)

    def test(self):
        self._ready()
        probe = self.base / (".claude-backup-test-%s" % uuid.uuid4().hex[:6])
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            raise DestinationError("Can't write to %s: %s" % (self.base, e))
        free = shutil.disk_usage(str(self.base)).free
        return "Ready. %.1f GB free." % (free / 1e9)

    def upload(self, path, progress=_noop):
        self._ready()
        path = Path(path)
        dest = self.base / path.name
        tmp = dest.with_name(dest.name + ".part")
        total, done = path.stat().st_size, 0
        with open(path, "rb") as r, open(tmp, "wb") as w:
            for chunk in iter(lambda: r.read(CHUNK), b""):
                w.write(chunk)
                done += len(chunk)
                progress(done, total, "Copying to " + self.name)
            w.flush()
            os.fsync(w.fileno())
        os.replace(tmp, dest)
        if dest.stat().st_size != total:
            raise DestinationError("Copy to %s is incomplete" % dest)
        return path.name

    def list(self):
        self._ready()
        out = []
        for p in self.base.glob("claude-backup-*" + EXT):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size,
                        "modified": _dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def download(self, name, dest_dir, progress=_noop):
        self._check_name(name)
        self._ready()
        src = self.base / name
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        total, done = src.stat().st_size, 0
        with open(src, "rb") as r, open(dest, "wb") as w:
            for chunk in iter(lambda: r.read(CHUNK), b""):
                w.write(chunk)
                done += len(chunk)
                progress(done, total, "Downloading " + name)
        return dest

    def delete(self, name):
        self._check_name(name)
        (self.base / name).unlink()


# --------------------------------------------------------------------------
# Amazon S3 and S3-compatible storage (Signature Version 4)
# --------------------------------------------------------------------------
UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _uri(s, keep_slash=True):
    return quote(s, safe=UNRESERVED + ("/" if keep_slash else ""))


def sigv4_headers(method, host, path, query, headers, payload_hash, access_key, secret_key, region,
                  service="s3", now=None, session_token=None):
    """Return headers (including Authorization) for an AWS Signature Version 4 request."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    h = {k.lower(): str(v).strip() for k, v in headers.items()}
    h["host"] = host
    h["x-amz-date"] = amz_date
    h["x-amz-content-sha256"] = payload_hash
    if session_token:
        h["x-amz-security-token"] = session_token
    signed = sorted(h)
    canonical_query = "&".join("%s=%s" % (_uri(k, False), _uri(str(v), False)) for k, v in sorted(query.items()))
    canonical = "\n".join([method, _uri(path), canonical_query,
                           "".join("%s:%s\n" % (k, h[k]) for k in signed), ";".join(signed), payload_hash])
    scope = "%s/%s/%s/aws4_request" % (day, region, service)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
        access_key, scope, ";".join(signed), signature)
    return h


def aws_profile(profile=None, home=None):
    """(access_key, secret_key, session_token, region) from env vars or ~/.aws files."""
    home = Path(home or Path.home())
    profile = profile or os.environ.get("AWS_PROFILE") or "default"
    if os.environ.get("AWS_ACCESS_KEY_ID") and not profile_explicit(profile):
        return (os.environ["AWS_ACCESS_KEY_ID"], os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
                os.environ.get("AWS_SESSION_TOKEN"), os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    cred = configparser.RawConfigParser()
    cred.read(str(home / ".aws" / "credentials"))
    conf = configparser.RawConfigParser()
    conf.read(str(home / ".aws" / "config"))
    csec = profile if profile == "default" else "profile " + profile
    region = conf.get(csec, "region", fallback=None) if conf.has_section(csec) else None
    if not cred.has_section(profile):
        if conf.has_section(csec) and (conf.has_option(csec, "sso_session") or conf.has_option(csec, "sso_start_url")):
            raise DestinationError("AWS profile '%s' uses SSO, which needs the AWS CLI. Use an access key, "
                                   "or the rclone option, instead." % profile)
        raise DestinationError("No AWS credentials found for profile '%s' in ~/.aws/credentials." % profile)
    return (cred.get(profile, "aws_access_key_id", fallback=""), cred.get(profile, "aws_secret_access_key", fallback=""),
            cred.get(profile, "aws_session_token", fallback=None), region)


def profile_explicit(profile):
    return profile not in (None, "", "default")


class S3Dest(Destination):
    type = "s3"

    def _creds(self):
        c = self.cfg
        if c.get("access_key"):
            secret = self.secrets.get(c["id"]) if self.secrets else None
            if not secret:
                raise DestinationError("The secret key for %s is missing. Edit the destination and enter it again." % self.name)
            return c["access_key"], secret, None, c.get("region")
        return aws_profile(c.get("profile"))

    def _target(self):
        c = self.cfg
        ak, sk, token, prof_region = self._creds()
        region = c.get("region") or prof_region or "us-east-1"
        bucket = c.get("bucket", "").strip()
        if not bucket:
            raise DestinationError("Enter a bucket name.")
        if c.get("endpoint"):
            u = urlparse(c["endpoint"] if "://" in c["endpoint"] else "https://" + c["endpoint"])
            scheme, host, base = u.scheme, u.netloc, "/" + bucket
        else:
            scheme, host, base = "https", "%s.s3.%s.amazonaws.com" % (bucket, region), ""
        return scheme, host, base, ak, sk, token, region

    def _key(self, name=""):
        prefix = (self.cfg.get("prefix") or "").strip("/")
        return (prefix + "/" + name) if prefix else name

    def _request(self, method, key, query=None, body_path=None, out=None, progress=_noop, label=""):
        scheme, host, base, ak, sk, token, region = self._target()
        path = base + "/" + key if key else (base or "/")
        query = query or {}
        size = 0
        if body_path:
            size = Path(body_path).stat().st_size
            hsh = hashlib.sha256()
            with open(body_path, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    hsh.update(chunk)
            payload = hsh.hexdigest()
        else:
            payload = EMPTY_SHA
        headers = {"content-length": str(size)} if method == "PUT" else {}
        if method == "PUT":
            headers["content-type"] = "application/octet-stream"
        h = sigv4_headers(method, host, path, query, headers, payload, ak, sk, region, session_token=token)
        url_path = _uri(path) + ("?" + "&".join("%s=%s" % (_uri(k, False), _uri(str(v), False))
                                                for k, v in sorted(query.items())) if query else "")
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, timeout=120)
        try:
            conn.putrequest(method, url_path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders()
            if body_path:
                done = 0
                with open(body_path, "rb") as f:
                    for chunk in iter(lambda: f.read(CHUNK), b""):
                        conn.send(chunk)
                        done += len(chunk)
                        progress(done, size, label)
            resp = conn.getresponse()
            if resp.status >= 300:
                raise DestinationError(_s3_error(resp.status, resp.read()))
            if out:
                total = int(resp.getheader("content-length") or 0)
                done = 0
                with open(out, "wb") as w:
                    for chunk in iter(lambda: resp.read(CHUNK), b""):
                        w.write(chunk)
                        done += len(chunk)
                        progress(done, total, label)
                return None
            return resp.read()
        except OSError as e:
            raise DestinationError("Couldn't reach %s: %s" % (host, e))
        finally:
            conn.close()

    def upload(self, path, progress=_noop):
        name = Path(path).name
        self._request("PUT", self._key(name), body_path=path, progress=progress, label="Uploading to " + self.name)
        return name

    def list(self):
        out, token = [], None
        prefix = self._key("")
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "", q))
            for c in root.iter(S3_NS + "Contents"):
                name = c.findtext(S3_NS + "Key", "")[len(prefix):]
                if NAME_RE.match(name):
                    out.append({"name": name, "size": int(c.findtext(S3_NS + "Size", "0")),
                                "modified": c.findtext(S3_NS + "LastModified", "")})
            if root.findtext(S3_NS + "IsTruncated") != "true":
                break
            token = root.findtext(S3_NS + "NextContinuationToken")
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def download(self, name, dest_dir, progress=_noop):
        self._check_name(name)
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._request("GET", self._key(name), out=dest, progress=progress, label="Downloading " + name)
        return dest

    def delete(self, name):
        self._check_name(name)
        self._request("DELETE", self._key(name))


def _s3_error(status, body):
    try:
        root = ET.fromstring(body)
        code, msg = root.findtext("Code"), root.findtext("Message")
        if code:
            hint = {"AccessDenied": " Check the key's permissions on this bucket.",
                    "NoSuchBucket": " Check the bucket name.",
                    "PermanentRedirect": " The bucket is in a different region; set the region.",
                    "AuthorizationHeaderMalformed": " Set the bucket's region.",
                    "SignatureDoesNotMatch": " Check the secret key.",
                    "InvalidAccessKeyId": " Check the access key ID."}.get(code, "")
            return "%s: %s%s" % (code, msg, hint)
    except ET.ParseError:
        pass
    return "HTTP %d from storage" % status


# --------------------------------------------------------------------------
# Azure Blob Storage (container SAS URL)
# --------------------------------------------------------------------------
AZ_VERSION = "2023-11-03"
AZ_SINGLE_MAX = 64 << 20
AZ_BLOCK = 8 << 20


class AzureDest(Destination):
    type = "azure"

    def _parts(self):
        url = (self.cfg.get("container_url") or "").split("?")[0].rstrip("/")
        sas = self.secrets.get(self.cfg["id"]) if self.secrets else None
        if not url:
            raise DestinationError("Enter the container URL.")
        if not sas:
            raise DestinationError("The SAS token for %s is missing. Edit the destination and paste the SAS URL again." % self.name)
        u = urlparse(url)
        return u.scheme, u.netloc, u.path, sas.lstrip("?")

    def _key(self, name=""):
        prefix = (self.cfg.get("prefix") or "").strip("/")
        return (prefix + "/" + name) if prefix else name

    def _req(self, method, blob, extra_query="", body=None, headers=None, out=None, progress=_noop, label=""):
        scheme, host, cpath, sas = self._parts()
        path = cpath + ("/" + _uri(blob) if blob else "")
        url = path + "?" + "&".join(x for x in (extra_query, sas) if x)
        conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, timeout=120)
        h = {"x-ms-version": AZ_VERSION}
        h.update(headers or {})
        try:
            if body is not None:
                h["Content-Length"] = str(len(body))
            conn.request(method, url, body=body, headers=h)
            resp = conn.getresponse()
            if resp.status >= 300:
                raise DestinationError(_az_error(resp.status, resp.read()))
            if out:
                total, done = int(resp.getheader("content-length") or 0), 0
                with open(out, "wb") as w:
                    for chunk in iter(lambda: resp.read(CHUNK), b""):
                        w.write(chunk)
                        done += len(chunk)
                        progress(done, total, label)
                return None
            return resp.read()
        except OSError as e:
            raise DestinationError("Couldn't reach %s: %s" % (host, e))
        finally:
            conn.close()

    def upload(self, path, progress=_noop):
        path = Path(path)
        name, size = path.name, path.stat().st_size
        blob = self._key(name)
        label = "Uploading to " + self.name
        with open(path, "rb") as f:
            if size <= AZ_SINGLE_MAX:
                data = f.read()
                self._req("PUT", blob, body=data, headers={"x-ms-blob-type": "BlockBlob",
                                                           "Content-Type": "application/octet-stream"})
                progress(size, size, label)
                return name
            ids, done = [], 0
            for n, chunk in enumerate(iter(lambda: f.read(AZ_BLOCK), b"")):
                bid = base64.b64encode(("%08d" % n).encode()).decode()
                self._req("PUT", blob, "comp=block&blockid=" + quote(bid, safe=""), body=chunk)
                ids.append(bid)
                done += len(chunk)
                progress(done, size, label)
        xml = "<?xml version=\"1.0\" encoding=\"utf-8\"?><BlockList>%s</BlockList>" % "".join(
            "<Latest>%s</Latest>" % i for i in ids)
        self._req("PUT", blob, "comp=blocklist", body=xml.encode(), headers={"Content-Type": "application/xml"})
        return name

    def list(self):
        out, marker = [], ""
        prefix = self._key("")
        while True:
            q = "restype=container&comp=list&prefix=" + quote(prefix, safe="") + (
                "&marker=" + quote(marker, safe="") if marker else "")
            root = ET.fromstring(self._req("GET", "", q))
            for b in root.iter("Blob"):
                name = (b.findtext("Name") or "")[len(prefix):]
                if NAME_RE.match(name):
                    props = b.find("Properties")
                    out.append({"name": name, "size": int(props.findtext("Content-Length", "0")) if props is not None else 0,
                                "modified": props.findtext("Last-Modified", "") if props is not None else ""})
            marker = root.findtext("NextMarker") or ""
            if not marker:
                break
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def download(self, name, dest_dir, progress=_noop):
        self._check_name(name)
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._req("GET", self._key(name), out=dest, progress=progress, label="Downloading " + name)
        return dest

    def delete(self, name):
        self._check_name(name)
        self._req("DELETE", self._key(name))


def _az_error(status, body):
    try:
        root = ET.fromstring(body)
        code, msg = root.findtext("Code"), (root.findtext("Message") or "").split("\n")[0]
        if code:
            hint = {"AuthenticationFailed": " Check the SAS token and its expiry date.",
                    "AuthorizationPermissionMismatch": " The SAS token needs Read, Add, Create, Write, Delete and List permissions.",
                    "ContainerNotFound": " Check the container name."}.get(code, "")
            return "%s: %s%s" % (code, msg, hint)
    except ET.ParseError:
        pass
    return "HTTP %d from Azure" % status


# --------------------------------------------------------------------------
# rclone: Google Drive, OneDrive, Dropbox, Box, pCloud, Mega, SFTP and more
# --------------------------------------------------------------------------
def rclone_path():
    for p in [shutil.which("rclone"), "/opt/homebrew/bin/rclone", "/usr/local/bin/rclone", "/usr/bin/rclone"]:
        if p and os.path.exists(p):
            return p
    return None


def rclone_remotes():
    exe = rclone_path()
    if not exe:
        return []
    r = subprocess.run([exe, "listremotes", "--long"], capture_output=True, text=True, timeout=30)
    out = []
    for line in r.stdout.splitlines():
        if ":" in line:
            name, _, kind = line.partition(":")
            out.append({"name": name.strip(), "type": kind.strip()})
    return out


class RcloneDest(Destination):
    type = "rclone"

    def _remote(self, name=""):
        remote = (self.cfg.get("remote") or "").strip()
        if ":" not in remote:
            raise DestinationError("Enter an rclone remote like  gdrive:Claude Backups")
        return remote.rstrip("/") + ("/" + name if name else "")

    def _run(self, *args, timeout=3600):
        exe = rclone_path()
        if not exe:
            raise DestinationError("rclone isn't installed. Install it from https://rclone.org/install/ "
                                   "(on a Mac: brew install rclone), then run  rclone config.")
        r = subprocess.run([exe] + list(args), capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise DestinationError("rclone: " + (r.stderr.strip().splitlines() or ["failed"])[-1])
        return r.stdout

    def test(self):
        self._run("mkdir", self._remote(), timeout=120)
        self.list()
        return "Connected."

    def upload(self, path, progress=_noop):
        name = Path(path).name
        size = Path(path).stat().st_size
        progress(0, size, "Uploading to " + self.name)
        self._run("copyto", str(path), self._remote(name))
        progress(size, size, "Uploading to " + self.name)
        return name

    def list(self):
        try:
            data = json.loads(self._run("lsjson", "--files-only", self._remote(), timeout=300) or "[]")
        except DestinationError as e:
            if "directory not found" in str(e):
                return []
            raise
        return sorted(({"name": d["Name"], "size": d.get("Size", 0), "modified": d.get("ModTime", "")}
                       for d in data if NAME_RE.match(d.get("Name", ""))), key=lambda x: x["name"], reverse=True)

    def download(self, name, dest_dir, progress=_noop):
        self._check_name(name)
        dest = Path(dest_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("copyto", self._remote(name), str(dest))
        return dest

    def delete(self, name):
        self._check_name(name)
        self._run("deletefile", self._remote(name), timeout=300)


TYPES = {"folder": FolderDest, "s3": S3Dest, "azure": AzureDest, "rclone": RcloneDest}

# What the app offers when adding a destination. "type" is how it's stored.
KINDS = {
    "external":     {"type": "folder", "label": "External drive or USB disk"},
    "google-drive": {"type": "folder", "label": "Google Drive (Drive for desktop app)"},
    "onedrive":     {"type": "folder", "label": "Microsoft OneDrive (OneDrive app)"},
    "dropbox":      {"type": "folder", "label": "Dropbox (Dropbox app)"},
    "icloud":       {"type": "folder", "label": "iCloud Drive"},
    "box":          {"type": "folder", "label": "Box (Box Drive app)"},
    "folder":       {"type": "folder", "label": "Any folder or network drive (NAS)"},
    "s3":           {"type": "s3", "label": "Amazon S3 or S3-compatible (Backblaze B2, Wasabi, R2, MinIO)"},
    "azure":        {"type": "azure", "label": "Microsoft Azure Blob Storage"},
    "rclone":       {"type": "rclone", "label": "Other cloud via rclone (Google Drive, OneDrive, Dropbox, Box, pCloud, Mega, SFTP...)"},
}


def make(cfg, secrets=None):
    cls = TYPES.get(cfg.get("type"))
    if not cls:
        raise DestinationError("Unknown destination type: %s" % cfg.get("type"))
    return cls(cfg, secrets)


def detect_locations(home=None):
    """Synced cloud folders and external drives found on this computer."""
    home = Path(home or Path.home())
    found = []
    system = platform.system()

    def add(kind, label, path):
        if Path(path).is_dir() and os.access(str(path), os.W_OK):
            found.append({"kind": kind, "label": label, "path": str(path)})

    if system == "Darwin":
        cs = home / "Library" / "CloudStorage"
        if cs.is_dir():
            for d in sorted(cs.iterdir()):
                n = d.name
                if n.startswith("GoogleDrive-"):
                    add("google-drive", "Google Drive (%s)" % n[len("GoogleDrive-"):],
                        d / "My Drive" if (d / "My Drive").is_dir() else d)
                elif n.startswith("OneDrive"):
                    add("onedrive", "OneDrive" + (" (%s)" % n.split("-", 1)[1] if "-" in n else ""), d)
                elif n.startswith("Dropbox"):
                    add("dropbox", "Dropbox", d)
                elif n.startswith("Box"):
                    add("box", "Box", d)
                else:
                    add("folder", n, d)
        add("icloud", "iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
        vols = Path("/Volumes")
        if vols.is_dir():
            for v in sorted(vols.iterdir()):
                if os.path.realpath(str(v)) != "/" and not v.name.startswith(".") and "TimeMachine" not in v.name:
                    add("external", "Drive: " + v.name, v)
    elif system == "Windows":
        for var, kind, label in (("OneDrive", "onedrive", "OneDrive"),
                                 ("OneDriveCommercial", "onedrive", "OneDrive (work or school)")):
            if os.environ.get(var):
                add(kind, label, os.environ[var])
        for p, kind, label in ((home / "Google Drive", "google-drive", "Google Drive"),
                               (Path("G:/My Drive"), "google-drive", "Google Drive (G:)"),
                               (home / "Dropbox", "dropbox", "Dropbox"),
                               (home / "iCloudDrive", "icloud", "iCloud Drive"),
                               (home / "Box", "box", "Box")):
            add(kind, label, p)
        import string
        for letter in string.ascii_uppercase[3:]:
            root = Path("%s:/" % letter)
            if root.exists() and letter != "G":
                add("external", "Drive %s:" % letter, root)
    else:
        user = getpass.getuser()
        for pat in ("/media/%s/*" % user, "/run/media/%s/*" % user, "/mnt/*"):
            for v in sorted(glob.glob(pat)):
                add("external", "Drive: " + os.path.basename(v), v)
        add("dropbox", "Dropbox", home / "Dropbox")
        add("google-drive", "Google Drive", home / "GoogleDrive")
        add("onedrive", "OneDrive", home / "OneDrive")
    return found


def copy_to(dests_cfg, path, secrets, progress=_noop, only_auto=True):
    """Copy a backup to destinations. Returns [{'name', 'ok', 'message'}]; never raises."""
    results = []
    for cfg in dests_cfg:
        if only_auto and not cfg.get("auto"):
            continue
        try:
            d = make(cfg, secrets)
            d.upload(path, progress)
            removed = d.prune(cfg.get("keep") or 0)
            msg = "Copied" + (" (removed %d older)" % len(removed) if removed else "")
            results.append({"id": cfg.get("id"), "name": d.name, "ok": True, "message": msg})
        except Exception as e:  # one failing destination must not stop the others
            if type(e).__name__ == "Cancelled":
                raise
            results.append({"id": cfg.get("id"), "name": cfg.get("name"), "ok": False, "message": str(e)})
    return results
