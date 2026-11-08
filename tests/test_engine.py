import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from claude_backup import engine
from claude_backup.env import Env

ACCT_A = "aaaaaaaa-1111-4111-8111-111111111111"
ORG_A = "bbbbbbbb-2222-4222-8222-222222222222"
ACCT_B = "cccccccc-3333-4333-8333-333333333333"
ORG_B = "dddddddd-4444-4444-8444-444444444444"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_home(root, acct, org, email, with_data=True):
    home = Path(root)
    desktop = home / "Desktop-Claude"
    write(home / ".claude.json", json.dumps({
        "oauthAccount": {"accountUuid": acct, "organizationUuid": org, "emailAddress": email},
        "userID": "secret-user-id",
        "mcpServers": {"github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_abcdefghijklmnop"}}},
        "projects": {"/work/app": {"allowedTools": ["Bash(ls)"], "history": ["x"]}},
    }))
    write(desktop / "config.json", json.dumps({"oauth:tokenCache": "SECRET", "lastKnownAccountUuid": acct}))
    write(desktop / "claude_desktop_config.json", json.dumps({
        "coworkUserFilesPath": "/nowhere",
        "preferences": {"sidebarMode": "chat", "modelByAccount": {acct: "opus"}},
        "mcpServers": {"files-" + email[0]: {"command": "fs-mcp", "env": {"API_KEY": "sk-1234567890abcdef"}}},
    }))
    write(desktop / "local-agent-mode-sessions" / acct / org / "scheduled-tasks.json",
          json.dumps({"scheduledTasks": [{"owner": acct}]}))
    if not with_data:
        return home, desktop
    sdir = desktop / "local-agent-mode-sessions" / "skills-plugin" / org / acct
    write(sdir / "manifest.json", json.dumps({"skills": [
        {"name": "my-skill", "creatorType": "user", "skillId": "skill_1", "description": "Mine"},
        {"name": "pdf", "creatorType": "anthropic", "skillId": "skill_2"},
    ]}))
    write(sdir / "skills" / "my-skill" / "SKILL.md", "---\nname: my-skill\ndescription: Mine\n---\nDo it.")
    write(sdir / "skills" / "my-skill" / "scripts" / "run.py", "print('hi')")
    write(sdir / "skills" / "pdf" / "SKILL.md", "anthropic managed")
    rpm = desktop / "local-agent-mode-sessions" / acct / org / "rpm"
    write(rpm / "manifest.json", json.dumps({"plugins": [
        {"id": "plugin_X", "name": "my-plugin", "installedBy": "user"},
        {"id": "plugin_Y", "name": "org-plugin", "installedBy": "org"},
    ]}))
    write(rpm / "plugin_X" / "README.md", "plugin")
    write(rpm / "plugin_Y" / "README.md", "org plugin")
    ch = home / ".claude"
    write(ch / "CLAUDE.md", "Be terse.")
    write(ch / "commands" / "ship.md", "Ship it")
    write(ch / "skills" / "cc-skill" / "SKILL.md", "---\ndescription: CC skill\n---\n")
    write(ch / "settings.json", json.dumps({"model": "opus", "env": {"ANTHROPIC_API_KEY": "sk-ant-xxxxxxxxxxxx"}}))
    write(ch / "projects" / "-work-app" / "s1.jsonl", '{"msg": "hello"}\n')
    write(ch / "projects" / "-work-app" / "memory" / "MEMORY.md", "- fact")
    write(ch / ".credentials.json", "TOP SECRET")
    return home, desktop


def make_export(path):
    conv = [{
        "uuid": "conv-1", "name": "Landing page", "created_at": "2026-01-02T03:04:05Z",
        "chat_messages": [
            {"sender": "human", "text": "Build a landing page", "created_at": "2026-01-02T03:04:05Z"},
            {"sender": "assistant", "content": [
                {"type": "text", "text": "Here you go"},
                {"type": "tool_use", "name": "artifacts", "input": {
                    "id": "landing", "command": "create", "type": "text/html", "title": "Landing Page",
                    "content": "<h1>Hello</h1>"}},
                {"type": "tool_use", "name": "artifacts", "input": {
                    "id": "landing", "command": "update", "old_str": "Hello", "new_str": "Hello world"}},
                {"type": "tool_use", "name": "artifacts", "input": {
                    "id": "calc", "command": "create", "type": "application/vnd.ant.code",
                    "language": "python", "title": "Calc", "content": "print(1+1)"}},
            ]},
            {"sender": "assistant", "text": 'Old: <antArtifact identifier="d" type="text/markdown" title="Notes">\n# Notes\n</antArtifact>'},
        ]}]
    projects = [{"uuid": "p1", "name": "Research", "prompt_template": "You are a researcher.",
                 "docs": [{"filename": "brief.md", "content": "Brief"}]}]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("conversations.json", json.dumps(conv))
        z.writestr("projects.json", json.dumps(projects))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home_a, self.desk_a = make_home(base / "a", ACCT_A, ORG_A, "a@example.com")
        self.env_a = Env(home=self.home_a, desktop=self.desk_a)
        self.out = base / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_only_user_content(self):
        r = engine.scan(self.env_a)
        ids = {i["id"] for i in r["items"]}
        self.assertIn("skill:my-skill", ids)
        self.assertIn("skill-cc:cc-skill", ids)
        self.assertNotIn("skill:pdf", ids)
        self.assertIn("plugin:my-plugin", ids)
        self.assertNotIn("plugin:org-plugin", ids)
        self.assertEqual(r["current_account"]["account"], ACCT_A)

    def test_backup_excludes_secrets_and_verifies(self):
        path, m = engine.backup(self.env_a, out_dir=self.out)
        self.assertEqual(engine.verify(path), [])
        with zipfile.ZipFile(path) as z:
            blob = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"TOP SECRET", blob)
        self.assertNotIn(b"oauth:tokenCache", blob)
        self.assertNotIn(b"secret-user-id", blob)
        self.assertNotIn(b"/nowhere", blob)
        with zipfile.ZipFile(path) as z:
            cj = json.loads(z.read("data/settings__claude-json/.claude.json"))
        self.assertEqual(set(cj), {"mcpServers", "projects"})
        self.assertNotIn("history", cj["projects"]["/work/app"])
        rels = {i["id"]: i["rel"] for i in m["items"]}
        self.assertIn("{account}", rels["skill:my-skill"])
        self.assertNotIn(ACCT_A, json.dumps(rels))

    def test_redaction(self):
        path, _ = engine.backup(self.env_a, ["settings"], out_dir=self.out, redact_secrets=True)
        with zipfile.ZipFile(path) as z:
            blob = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"ghp_abcdefghijklmnop", blob)
        self.assertNotIn(b"sk-1234567890abcdef", blob)
        self.assertIn(b"<REDACTED>", blob)

    def test_restore_into_other_account_on_other_machine(self):
        path, _ = engine.backup(self.env_a, out_dir=self.out)
        home_b, desk_b = make_home(Path(self.tmp.name) / "b", ACCT_B, ORG_B, "b@example.com", with_data=False)
        env_b = Env(home=home_b, desktop=desk_b)
        res = engine.restore(env_b, path, skill_targets=["claude-code", "upload", "desktop"], out_dir=self.out)
        self.assertEqual(res["target"], {"account": ACCT_B, "org": ORG_B})
        # Skill installed for Claude Code, cached for Desktop under B, and zipped for upload
        self.assertTrue((home_b / ".claude" / "skills" / "my-skill" / "scripts" / "run.py").exists())
        sdir = env_b.skills_dir(ACCT_B, ORG_B)
        self.assertTrue((sdir / "skills" / "my-skill" / "SKILL.md").exists())
        names = [s["name"] for s in json.loads((sdir / "manifest.json").read_text())["skills"]]
        self.assertEqual(names, ["my-skill"])
        zips = list(Path(res["out_dir"]).glob("skills-to-upload/*.zip"))
        self.assertEqual({z.stem for z in zips}, {"my-skill", "cc-skill"})
        with zipfile.ZipFile(Path(res["out_dir"]) / "skills-to-upload" / "my-skill.zip") as z:
            self.assertIn("my-skill/SKILL.md", z.namelist())
        # Account IDs in content are rewritten to the target account
        tasks = (env_b.account_dir(ACCT_B, ORG_B) / "scheduled-tasks.json").read_text()
        self.assertIn(ACCT_B, tasks)
        self.assertNotIn(ACCT_A, tasks)
        cfg = json.loads((desk_b / "claude_desktop_config.json").read_text())
        self.assertIn(ACCT_B, cfg["preferences"]["modelByAccount"])
        self.assertIn("files-a", cfg["mcpServers"])
        self.assertIn("files-b", cfg["mcpServers"])
        # ~/.claude.json merged, B's sign-in untouched
        cj = json.loads((home_b / ".claude.json").read_text())
        self.assertEqual(cj["oauthAccount"]["accountUuid"], ACCT_B)
        self.assertEqual(cj["projects"]["/work/app"]["allowedTools"], ["Bash(ls)"])
        self.assertEqual((home_b / ".claude" / "CLAUDE.md").read_text(), "Be terse.")
        self.assertTrue((home_b / ".claude" / "projects" / "-work-app" / "memory" / "MEMORY.md").exists())

        # Undo returns B to how it was
        engine.undo(res["snapshot"])
        self.assertFalse((home_b / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((home_b / ".claude" / "skills").exists())
        cfg = json.loads((desk_b / "claude_desktop_config.json").read_text())
        self.assertNotIn("files-a", cfg["mcpServers"])
        self.assertIn("files-b", cfg["mcpServers"])
        tasks = (env_b.account_dir(ACCT_B, ORG_B) / "scheduled-tasks.json").read_text()
        self.assertIn(ACCT_B, tasks)

    def test_dry_run_and_skip_existing(self):
        path, _ = engine.backup(self.env_a, ["prompts"], out_dir=self.out)
        write(self.home_a / ".claude" / "CLAUDE.md", "changed")
        res = engine.restore(self.env_a, path, dry_run=True, out_dir=self.out)
        self.assertTrue(any(a["action"] == "overwrite" for a in res["actions"]))
        self.assertEqual((self.home_a / ".claude" / "CLAUDE.md").read_text(), "changed")
        engine.restore(self.env_a, path, conflict="skip", out_dir=self.out)
        self.assertEqual((self.home_a / ".claude" / "CLAUDE.md").read_text(), "changed")
        engine.restore(self.env_a, path, out_dir=self.out)
        self.assertEqual((self.home_a / ".claude" / "CLAUDE.md").read_text(), "Be terse.")

    def test_migrate_between_local_accounts(self):
        write(self.desk_a / "local-agent-mode-sessions" / ACCT_B / ORG_B / "scheduled-tasks.json", "{}")
        res = engine.migrate(self.env_a, (ACCT_A, ORG_A), (ACCT_B, ORG_B), ["skills", "settings"],
                             out_dir=self.out)
        sdir = self.env_a.skills_dir(ACCT_B, ORG_B)
        self.assertTrue((sdir / "skills" / "my-skill" / "SKILL.md").exists())
        self.assertIn(ACCT_B, (self.env_a.account_dir(ACCT_B, ORG_B) / "scheduled-tasks.json").read_text())
        self.assertTrue(Path(res["backup"]).exists())

    def test_claude_ai_export(self):
        exp = Path(self.tmp.name) / "export.zip"
        make_export(exp)
        path, _ = engine.backup(self.env_a, ["artifacts", "prompts"], export_zip=exp, out_dir=self.out)
        res = engine.restore(self.env_a, path, out_dir=self.out)
        arts = Path(res["out_dir"]) / "claude-ai" / "artifacts"
        page = arts / "2026-01-02_Landing-page" / "Landing-Page.html"
        self.assertEqual(page.read_text(), "<h1>Hello world</h1>")
        self.assertEqual((arts / "2026-01-02_Landing-page" / "Calc.py").read_text(), "print(1+1)")
        self.assertTrue((arts / "2026-01-02_Landing-page" / "Notes.md").exists())
        prompts = Path(res["out_dir"]) / "claude-ai" / "prompts"
        self.assertEqual((prompts / "projects" / "Research" / "instructions.md").read_text(),
                         "You are a researcher.")
        self.assertIn("Build a landing page", (prompts / "prompt-history.jsonl").read_text())

    def test_rejects_non_backup(self):
        bad = Path(self.tmp.name) / "x.claudebackup"
        bad.write_bytes(b"nope")
        with self.assertRaises(engine.BackupError):
            engine.inspect(bad)


if __name__ == "__main__":
    unittest.main()
