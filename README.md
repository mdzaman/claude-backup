# Claude Backup

Back up, restore and migrate your **Claude Desktop** and **Claude Code** data: skills, plugins, prompts, artifacts, settings and history. Restore into **any** Claude account, on this computer or another one, and keep copies on an **external drive** or in **Google Drive, OneDrive, Dropbox, iCloud, Box, Amazon S3, Azure** or 70+ other clouds.

> Independent open-source project. Not affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of Anthropic, PBC.

- **No dependencies.** Python 3.8+ standard library only. Runs on macOS, Windows and Linux.
- **Runs in your browser, on any screen.** A fully responsive local app with a guided migration wizard and a full command line. Pair an iPhone or Android phone as a home-screen remote.
- **Safe by default.** Preview every restore, undo any restore, verify checksums, and sign-in tokens are never backed up.

## Features

| | |
|---|---|
| **Back up** | Whole categories or single items (one skill, one project), for one account or every account on the computer |
| **Restore** | Into the signed-in account or any other; account and org IDs are rewritten. Preview first, undo after |
| **Move accounts** | Step-by-step wizard for a new account or a new computer, ending with an upload checklist for skills and plugins |
| **Cloud & drives** | External drives, synced folders (Google Drive, OneDrive, Dropbox, iCloud, Box), Amazon S3 and S3-compatible storage (Backblaze B2, Wasabi, R2, MinIO), Azure Blob Storage, and any rclone remote. Copy every backup automatically, keep the newest N, and download and restore from any of them |
| **Artifacts** | Rebuilds every artifact from a claude.ai data export at its final version, as real files |
| **Automatic** | Daily or weekly backups with launchd, Task Scheduler or cron, even when the app is closed |
| **Library** | Browse inside backups, preview files, verify, extract, copy to a cloud |
| **Phone** | Optional, off by default: scan a QR code to use the computer's app from an iPhone or Android phone over Wi-Fi (HTTPS, separate key, no restores or deletes) |
| **Undo** | Every restore saves a snapshot of the files it changes, so it can be undone |

## Quick start

```bash
git clone https://github.com/mdzaman/claude-backup.git
cd claude-backup
python3 -m claude_backup
```

The app opens in your browser. Click **Back up everything now**, then add a drive or cloud under **Cloud & drives**.

On a Mac, `python3 -m claude_backup make-app` builds a double-clickable `dist/Claude Backup.app`. On Windows, double-click `Claude Backup.bat`. To install it as a command, run `pip install .`, which gives you `claude-backup`.

**Full instructions:** [User guide](claude_backup/docs/USER_GUIDE.md). It covers every screen, setup for each cloud, the command line, security and troubleshooting. The same guide is on the app's **Help** screen.

## What gets backed up

| Category | Contents |
|---|---|
| Skills | Skills you created in Claude Desktop or claude.ai; Claude Code skills (`~/.claude/skills`) |
| Plugins | Plugins you uploaded or installed; Cowork marketplaces; Claude Code plugins |
| Prompts | `CLAUDE.md`, slash commands, subagents, output styles, project memory; claude.ai project instructions, knowledge and prompt history (from an export) |
| Artifacts | All artifacts from a claude.ai export, at their final versions; Cowork output files |
| Settings | MCP servers and preferences, Claude Code settings and per-project permissions, Cowork settings and scheduled tasks |
| History | Claude Code transcripts, todos and plans; claude.ai conversations (from an export) |

Never backed up: sign-in tokens, credentials, cookies, caches and Claude Desktop's VM image.

claude.ai conversations, projects and artifacts live on Anthropic's servers. Include them by adding a claude.ai data export (**Settings → Privacy → Export data** in Claude).

## Moving to a new account: why uploading is needed

Skills and plugins you create in Claude Desktop or claude.ai are stored in your Claude account in the cloud. Claude Backup restores their files locally and installs skills into Claude Code, which works for any account. It also writes upload-ready zips. To make them appear in claude.ai and Claude Desktop under a new account, upload those zips in **Settings → Capabilities → Skills**. The app gives you a checklist for this.

## Command line

```bash
python3 -m claude_backup backup                 # full backup, copied to your "every backup" destinations
python3 -m claude_backup restore FILE --dry-run
python3 -m claude_backup destinations --detect
python3 -m claude_backup remote "My S3"
python3 -m claude_backup --help
```

See [Command line](claude_backup/docs/USER_GUIDE.md#command-line) for every command.

## How it works

A backup is a zip (`*.claudebackup`) holding `manifest.json` and a `data/` tree. Each item records a logical root (`CLAUDE_HOME`, `DESKTOP`, `HOME`, `COWORK_FILES`), a path template in which account and organization IDs are written as `{account}` and `{org}`, a restore strategy (`copy`, `json-merge`, `claude-json`, `skill`, `plugin`, `export`), and a SHA-256 hash per file. Nothing depends on absolute paths, so backups move between computers and operating systems.

| Module | Purpose |
|---|---|
| `claude_backup/engine.py` | Collect, back up, inspect, verify, plan and apply restores, undo, migrate |
| `claude_backup/env.py` | Where Claude keeps its data on each OS; accounts on this computer |
| `claude_backup/claude_ai_export.py` | Rebuild artifacts, prompts and projects from a claude.ai export |
| `claude_backup/destinations.py` | Folder, S3 (SigV4), Azure Blob (SAS) and rclone destinations |
| `claude_backup/web.py`, `ui.html` | Local server with background tasks, and the browser app |
| `claude_backup/mobile.py` | Phone pairing, HTTPS, QR code and the phone's allowed actions |
| `claude_backup/schedule.py` | launchd, Task Scheduler and cron |
| `claude_backup/cli.py` | Command line |
| `claude_backup/gui.py` | Older Tk window (`python3 -m claude_backup gui`) |

Claude's local storage layout isn't a published interface and may change between Claude versions. If something moves, `scan` shows missing categories; please open an issue.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests build fake Claude installs, so they never touch your real data. They cover the backup engine, the HTTP API end to end, S3 signing (against AWS's published test vector) with a local fake S3 server, Azure Blob against a fake server, folder destinations, rclone (mocked), and the Tk window when Tk is available. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
