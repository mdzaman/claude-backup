"""Destinations: folder, S3 (against a fake S3 server), Azure Blob (fake server), rclone (fake binary)."""
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

from claude_backup import destinations as D
from claude_backup.secrets import Secrets

NAME = "claude-backup-20260101-000000-full.claudebackup"
NAME2 = "claude-backup-20260102-000000-full.claudebackup"


class FakeStore(BaseHTTPRequestHandler):
    """Minimal S3 (path-style) and Azure Blob server keeping objects in memory."""
    objects = {}
    blocks = {}
    seen = []

    def log_message(self, *a):
        pass

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, code, data=b"", ctype="application/xml"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _azure(self):
        return self.path.startswith("/container")

    def do_PUT(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        key = unquote(u.path)
        body = self._body()
        FakeStore.seen.append((self.command, key, dict(self.headers), q))
        if self._azure():
            if "sig" not in q:
                return self._send(403, b"<Error><Code>AuthenticationFailed</Code><Message>no sas</Message></Error>")
            if q.get("comp") == ["block"]:
                FakeStore.blocks.setdefault(key, {})[q["blockid"][0]] = body
            elif q.get("comp") == ["blocklist"]:
                ids = re.findall(r"<Latest>([^<]+)</Latest>", body.decode())
                FakeStore.objects[key] = b"".join(FakeStore.blocks[key][i] for i in ids)
            else:
                FakeStore.objects[key] = body
            return self._send(201)
        if not self.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256"):
            return self._send(403, b"<Error><Code>AccessDenied</Code><Message>unsigned</Message></Error>")
        if hashlib.sha256(body).hexdigest() != self.headers.get("x-amz-content-sha256"):
            return self._send(400, b"<Error><Code>XAmzContentSHA256Mismatch</Code><Message>bad</Message></Error>")
        FakeStore.objects[key] = body
        self._send(200)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        key = unquote(u.path)
        if self._azure() and q.get("comp") == ["list"]:
            prefix = "/container/" + q.get("prefix", [""])[0]
            blobs = "".join("<Blob><Name>%s</Name><Properties><Content-Length>%d</Content-Length>"
                            "<Last-Modified>Thu, 01 Jan 2026 00:00:00 GMT</Last-Modified></Properties></Blob>"
                            % (k[len("/container/"):], len(v)) for k, v in FakeStore.objects.items() if k.startswith(prefix))
            return self._send(200, ("<EnumerationResults><Blobs>%s</Blobs><NextMarker/></EnumerationResults>" % blobs).encode())
        if not self._azure() and q.get("list-type") == ["2"]:
            bucket = key.rstrip("/")
            prefix = bucket + "/" + q.get("prefix", [""])[0]
            ns = "http://s3.amazonaws.com/doc/2006-03-01/"
            contents = "".join("<Contents><Key>%s</Key><Size>%d</Size><LastModified>2026-01-01T00:00:00Z</LastModified></Contents>"
                               % (k[len(bucket) + 1:], len(v)) for k, v in FakeStore.objects.items() if k.startswith(prefix))
            return self._send(200, ('<ListBucketResult xmlns="%s"><IsTruncated>false</IsTruncated>%s</ListBucketResult>'
                                    % (ns, contents)).encode())
        if key in FakeStore.objects:
            return self._send(200, FakeStore.objects[key], "application/octet-stream")
        self._send(404, b"<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>")

    def do_DELETE(self):
        FakeStore.objects.pop(unquote(urlparse(self.path).path), None)
        self._send(204)


class DestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeStore)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeStore.objects.clear()
        FakeStore.blocks.clear()
        FakeStore.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        os.environ["CLAUDE_BACKUP_SECRETS"] = "file"
        self.secrets = Secrets(self.base / "state")
        self.backup = self.base / NAME
        self.backup.write_bytes(os.urandom(300_000))

    def tearDown(self):
        os.environ.pop("CLAUDE_BACKUP_SECRETS", None)
        self.tmp.cleanup()

    def roundtrip(self, dest):
        self.assertTrue(dest.test())
        self.assertEqual(dest.upload(self.backup), NAME)
        listed = dest.list()
        self.assertEqual([b["name"] for b in listed], [NAME])
        self.assertEqual(listed[0]["size"], self.backup.stat().st_size)
        out = dest.download(NAME, self.base / "down")
        self.assertEqual(out.read_bytes(), self.backup.read_bytes())
        newer = self.base / NAME2
        newer.write_bytes(b"newer")
        dest.upload(newer)
        self.assertEqual(dest.prune(1), [NAME])
        self.assertEqual([b["name"] for b in dest.list()], [NAME2])
        dest.delete(NAME2)
        self.assertEqual(dest.list(), [])
        with self.assertRaises(D.DestinationError):
            dest.delete("../../etc/passwd")

    def test_sigv4_matches_aws_example(self):
        h = D.sigv4_headers("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
                            D.EMPTY_SHA, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                            "us-east-1", now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
        self.assertTrue(h["authorization"].endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_folder(self):
        dest = D.make({"type": "folder", "name": "USB", "path": str(self.base / "drive")})
        (self.base / "drive").mkdir()
        self.roundtrip(dest)
        (self.base / "drive" / "Claude Backups" / "notes.txt").write_text("keep me")
        dest.prune(0)
        self.assertTrue((self.base / "drive" / "Claude Backups" / "notes.txt").exists())
        missing = D.make({"type": "folder", "name": "Gone", "path": str(self.base / "unplugged")})
        with self.assertRaises(D.DestinationError) as e:
            missing.upload(self.backup)
        self.assertIn("connected", str(e.exception))

    def test_s3_compatible_endpoint(self):
        cfg = {"id": "s3x", "type": "s3", "name": "MinIO", "bucket": "bk", "prefix": "claude",
               "region": "us-east-1", "endpoint": "http://127.0.0.1:%d" % self.port, "access_key": "AKIA"}
        self.secrets.set("s3x", "secret")
        self.roundtrip(D.make(cfg, self.secrets))
        put = [s for s in FakeStore.seen if s[0] == "PUT"][0]
        self.assertEqual(put[1], "/bk/claude/" + NAME)

    def test_s3_missing_secret_and_profile(self):
        cfg = {"id": "nosecret", "type": "s3", "name": "S3", "bucket": "b", "access_key": "AKIA"}
        with self.assertRaises(D.DestinationError):
            D.make(cfg, self.secrets).list()
        aws = self.base / "home" / ".aws"
        aws.mkdir(parents=True)
        (aws / "credentials").write_text("[work]\naws_access_key_id = AK\naws_secret_access_key = SK\n")
        (aws / "config").write_text("[profile work]\nregion = eu-west-1\n[profile sso]\nsso_session = x\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            self.assertEqual(D.aws_profile("work", home=self.base / "home"), ("AK", "SK", None, "eu-west-1"))
            with self.assertRaises(D.DestinationError) as e:
                D.aws_profile("sso", home=self.base / "home")
            self.assertIn("SSO", str(e.exception))

    def test_azure_single_and_blocks(self):
        cfg = {"id": "az", "type": "azure", "name": "Azure",
               "container_url": "http://127.0.0.1:%d/container" % self.port, "prefix": "claude"}
        self.secrets.set("az", "sv=2023&sig=abc")
        self.roundtrip(D.make(cfg, self.secrets))
        with mock.patch.object(D, "AZ_SINGLE_MAX", 1000), mock.patch.object(D, "AZ_BLOCK", 64_000):
            dest = D.make(cfg, self.secrets)
            dest.upload(self.backup)
            self.assertEqual(FakeStore.objects["/container/claude/" + NAME], self.backup.read_bytes())
            self.assertGreater(sum(1 for s in FakeStore.seen if s[3].get("comp") == ["block"]), 3)
        self.secrets.delete("az")
        with self.assertRaises(D.DestinationError):
            D.make(cfg, self.secrets).list()

    def test_rclone_with_fake_binary(self):
        store = self.base / "rclone-store"
        store.mkdir()
        fake = self.base / "rclone"
        fake.write_text("""#!%s
import json, os, shutil, sys
root = %r
def local(remote):
    name, _, path = remote.partition(":")
    return os.path.join(root, path)
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "mkdir":
    os.makedirs(local(args[0]), exist_ok=True)
elif cmd == "copyto":
    src = local(args[0]) if ":" in args[0] and not os.path.exists(args[0]) else args[0]
    dst = local(args[1]) if ":" in args[1] and not os.path.isabs(args[1]) else args[1]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
elif cmd == "lsjson":
    d = local(args[-1])
    if not os.path.isdir(d):
        sys.stderr.write("error: directory not found\\n"); sys.exit(3)
    print(json.dumps([{"Name": n, "Size": os.path.getsize(os.path.join(d, n)), "ModTime": "2026-01-01T00:00:00Z"}
                      for n in os.listdir(d)]))
elif cmd == "deletefile":
    os.remove(local(args[0]))
""" % (sys.executable, str(store)))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch.object(D, "rclone_path", return_value=str(fake)):
            self.roundtrip(D.make({"type": "rclone", "name": "GDrive", "remote": "gdrive:Claude Backups"}))
        with mock.patch.object(D, "rclone_path", return_value=None):
            with self.assertRaises(D.DestinationError) as e:
                D.make({"type": "rclone", "remote": "x:y"}).list()
            self.assertIn("install", str(e.exception).lower())

    def test_copy_to_keeps_going_when_one_fails(self):
        (self.base / "good").mkdir()
        dests = [{"id": "1", "type": "folder", "name": "Gone", "path": str(self.base / "nope"), "auto": True},
                 {"id": "2", "type": "folder", "name": "Good", "path": str(self.base / "good"), "auto": True, "keep": 5},
                 {"id": "3", "type": "folder", "name": "Manual", "path": str(self.base / "good"), "auto": False}]
        res = D.copy_to(dests, self.backup, self.secrets)
        self.assertEqual([(r["name"], r["ok"]) for r in res], [("Gone", False), ("Good", True)])
        self.assertTrue((self.base / "good" / "Claude Backups" / NAME).exists())

    def test_secrets_file_is_private(self):
        self.secrets.set("k", "v")
        self.assertEqual(self.secrets.get("k"), "v")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.base / "state" / "secrets.json").stat().st_mode), 0o600)
        self.secrets.delete("k")
        self.assertIsNone(self.secrets.get("k"))

    def test_detect_locations(self):
        home = self.base / "home"
        (home / "Library" / "CloudStorage" / "GoogleDrive-me@example.com" / "My Drive").mkdir(parents=True)
        (home / "Library" / "CloudStorage" / "OneDrive-Contoso").mkdir(parents=True)
        with mock.patch.object(D.platform, "system", return_value="Darwin"):
            found = {f["kind"]: f for f in D.detect_locations(home)}
        self.assertTrue(found["google-drive"]["path"].endswith("My Drive"))
        self.assertEqual(found["onedrive"]["label"], "OneDrive (Contoso)")


if __name__ == "__main__":
    unittest.main()
