# Claude Backup user guide

Claude Backup saves your Claude Desktop and Claude Code data (skills, plugins, prompts, artifacts, settings and history) to a single backup file. It can restore that file into any Claude account, on this computer or another one, and keep copies on an external drive or in the cloud.

> Claude Backup is an independent open-source project. It isn't made or supported by Anthropic.

## Contents

- [Install and start](#install-and-start)
- [Quick start](#quick-start)
- [What gets backed up](#what-gets-backed-up)
- [Artifacts and chats from claude.ai](#artifacts-and-chats-from-claudeai)
- [Back up](#back-up)
- [Restore](#restore)
- [Move to another account](#move-to-another-account)
- [Upload skills and plugins to an account](#upload-skills-and-plugins-to-an-account)
- [Cloud and drives](#cloud-and-drives)
- [Automatic backups](#automatic-backups)
- [Library](#library)
- [Artifacts screen](#artifacts-screen)
- [Undo a restore](#undo-a-restore)
- [Settings and where files live](#settings-and-where-files-live)
- [Command line](#command-line)
- [Security and privacy](#security-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

## Install and start

**What you need:** Python 3.8 or newer. Nothing else: Claude Backup uses only Python's standard library. Python 3.15 is recommended.

**Get the code:**

```
git clone https://github.com/mdzaman/claude-backup.git
cd claude-backup
```

Or download the ZIP from GitHub (Code → Download ZIP) and unzip it.

**Install Python if you need it:**

- macOS with Homebrew: `brew install python@3.15`, or download the installer from python.org
- Windows: install from python.org (tick "Add python.exe to PATH"), or `winget install Python.Python.3.15`
- Linux: use your package manager, for example `sudo apt install python3`

**Start the app:**

| Platform | How |
|---|---|
| macOS (recommended) | Run `python3 -m claude_backup make-app` once in the project folder, then double-click `dist/Claude Backup.app`. Drag it to Applications if you like. |
| macOS (no app) | Double-click `Claude Backup.command` |
| Windows | Double-click `Claude Backup.bat` |
| Any | `python3 -m claude_backup` in the project folder |
| Installed with pip | `pip install .` in the project folder, then run `claude-backup` |

Claude Backup opens in your web browser. Behind the page, a small server runs on your computer only (`127.0.0.1`). Nothing is sent anywhere unless you add a cloud destination.

- Opening the app again while it's running brings back the same page instead of starting a second copy.
- To stop it, use **Settings → Quit Claude Backup**, or close the tab: it stops by itself after 15 minutes with no page open and no task running.
- `make-app` uses the newest Python it finds. If you install a newer Python, or move the project folder, run `make-app` again.

## Quick start

1. Open Claude Backup. The **Dashboard** shows what it found on this computer.
2. Click **Back up everything now**. The backup goes to `~/ClaudeBackups`.
3. Go to **Cloud & drives** and add an external drive or a cloud, with **Copy every new backup here** ticked. From then on, every backup is copied there too.
4. Go to **Automatic** and turn on a daily backup.

To move to another account, use **Move accounts**. It walks you through every step.

## What gets backed up

| Category | What's in it |
|---|---|
| **Skills** | Skills you created in Claude Desktop or claude.ai (not Anthropic's built-in ones), and Claude Code skills in `~/.claude/skills` |
| **Plugins** | Plugins you uploaded or installed in Claude Desktop, Cowork plugin marketplaces, and Claude Code plugins |
| **Prompts** | Global instructions (`~/.claude/CLAUDE.md`), slash commands, subagents, output styles, project memory, and, from a claude.ai export, project instructions, project knowledge files and your prompt history |
| **Artifacts** | Every artifact from a claude.ai export at its final version, and Cowork output files |
| **Settings** | MCP servers and preferences (`claude_desktop_config.json`), Claude Code `settings.json`, keybindings, MCP servers and per-project permissions from `~/.claude.json`, Cowork settings and scheduled tasks |
| **History** | Claude Code transcripts, todos and plans, desktop Code session data, and claude.ai conversations from an export |

Ticking every category is a **full backup**. You can also open a category and pick single items, such as one skill or one project's transcripts.

**Never backed up:** sign-in and OAuth tokens, `.credentials.json`, cookies, device keys, browser caches, and Claude Desktop's virtual machine image (about 10 GB). A restore never changes which account you're signed in to.

## Artifacts and chats from claude.ai

Your claude.ai conversations, projects and artifacts are stored on Anthropic's servers, not on your computer. Claude can export them for you:

1. In Claude, open **Settings → Privacy** and click **Export data**.
2. You'll get an email with a download link. Download the `.zip`.
3. In Claude Backup, choose it under **claude.ai export** on the **Back up** screen, or open it on the **Artifacts** screen.

Claude Backup replays each artifact's edits, so you get the final version of every artifact as its own file: `.html`, `.jsx`, `.py`, `.md`, `.svg`, `.mmd` and so on. You also get each project's instructions and knowledge files, and a `prompt-history.jsonl` with every message you sent.

## Back up

1. Open **Back up**.
2. Tick what to include. Open a category to pick single items. The total is shown at the top right.
3. Choose **Signed-in account**, or **Every account on this computer** if several Claude accounts have signed in here.
4. Optionally choose your **claude.ai export** zip.
5. Optionally tick **Remove API keys and tokens from MCP server settings**. Use this if you'll share the backup or keep it somewhere less private.
6. Click **Create backup**.

When it's done, the backup is copied to every destination marked **Copy every new backup here**, and the results are shown.

Backups are single files named like `claude-backup-20261107-202133-full.claudebackup`. Each one is a zip with a manifest and a checksum for every file.

## Restore

1. Open **Restore** and choose a backup: from the list, **Open file…**, or **From a drive or cloud…**.
2. Read the summary. It says where and when the backup was made, and whether it came from a different account.
3. Tick the items to restore.
4. Choose **Restore into**: the signed-in account, another account on this computer, or **Another account (enter its IDs)**.
5. Choose where skills go:
   - **Install into ~/.claude/skills.** They work right away in Claude Code, under any account.
   - **Write .zip files to upload.** For Claude Desktop and claude.ai (see [Upload skills and plugins to an account](#upload-skills-and-plugins-to-an-account)).
   - **Write into the Claude Desktop local skills cache.** Quick, but Claude may replace it the next time it syncs.
6. Choose what happens to files that already exist: **Replace** or **Keep existing**.
7. Click **Preview changes** to see every file that would be created, replaced or merged. Nothing is changed yet.
8. **Quit Claude Desktop** (Claude → Quit), then click **Restore**.

When it finishes, you get a checklist for uploading skills and plugins, and an **Undo this restore** button.

Config files are merged, not replaced. MCP servers and preferences from the backup are added to the target's settings, and the target's sign-in is never touched. If the backup had API keys removed, re-enter them in your MCP settings after restoring.

## Move to another account

Claude Desktop stores local data in folders named after your **account ID** and **organization ID**. Backups store these as placeholders, so a restore rewrites them to the target account's IDs, in folder names and inside files.

Open **Move accounts** and choose one of two routes.

**A new account, or another computer:**

1. **Cloud data:** optionally request a claude.ai export from the old account and choose the zip.
2. **What to move:** pick the categories and where skills should go.
3. **Back up:** creates the migration backup from the account that's signed in now.
   - Moving to another computer? Copy the backup file there, or add a cloud destination so it's copied for you. Open Claude Backup on the new computer and use **Restore**.
4. **Sign in:** sign out of Claude Desktop and sign in to the new account. If you use Claude Code, run `claude` and `/login` with the new account. Quit Claude Desktop, then click **Check again**. The app confirms you're on the new account.
5. **Restore:** preview, then restore into the new account.
6. **Upload:** upload your skills and plugins using the checklist.

**An account that has already signed in on this computer:** choose the accounts to copy from and into, pick what to move, preview, and click **Migrate now**. A backup of the source account is saved first.

## Upload skills and plugins to an account

Skills and plugins you create in Claude Desktop or claude.ai are stored in your Claude account in the cloud. A copy on your computer isn't enough for them to appear in a different account, so restores write upload-ready files:

- `skills-to-upload/<skill name>.zip`
- `plugins-to-upload/<plugin name>.zip`

These sit in a `restored-<date>` folder inside your backup folder. To upload them:

1. In the finish dialog, click **Open Claude skills settings**, or in Claude go to **Settings → Capabilities → Skills**.
2. Click **Upload skill** and choose each zip.
3. For plugins, go to **Customize → Plugins** and upload each plugin zip.
4. Tick each file off in the checklist as you go. Ticks are remembered.
5. Restart Claude Desktop.

Claude Code skills don't need uploading. They're installed straight into `~/.claude/skills`.

## Cloud and drives

A backup that lives only on your computer is lost along with the computer. Add at least one destination off this computer: open **Cloud & drives** and click **Add a drive or cloud**. Locations found on this computer, such as plugged-in drives and Google Drive, OneDrive, Dropbox and iCloud folders, are listed at the top.

Every destination has:

- **Copy every new backup here automatically.** Applies to backups made in the app, on a schedule, and from the command line.
- **Keep newest N.** Older backups are deleted from that destination (0 keeps all). Only files named `claude-backup-*.claudebackup` are ever deleted.
- **Test**, **Browse** (list, download, download and restore, delete), **Copy latest**, **Edit** and **Remove**. Removing a destination doesn't delete the backups stored there.

Passwords, secret keys and SAS tokens are kept in your macOS Keychain. On Windows and Linux they're in `~/.claude-backup/secrets.json`, readable only by you. They're never written to the settings file.

### External hard drive or USB disk

1. Plug in the drive.
2. **Add a drive or cloud**, then click the drive under **Found on this computer**, or choose **External drive or USB disk** and pick it.
3. Backups go into a `Claude Backups` folder on the drive.

If the drive isn't plugged in when a backup runs, the backup is still saved locally, and the copy is reported as failed. Use **Copy latest** when it's back.

### Google Drive

**With the Google Drive for desktop app (simplest):**

1. Install it from https://www.google.com/drive/download/ and sign in.
2. In Claude Backup, choose your Google Drive under **Found on this computer**, or choose **Google Drive** and pick the `My Drive` folder.
   - On a Mac it's in `~/Library/CloudStorage/GoogleDrive-<you>/My Drive`.
   - On Windows it's usually `G:\My Drive`.
3. The app copies backups into `My Drive/Claude Backups`, and Google Drive uploads them.

**Without the app:** use [rclone](#any-other-cloud-with-rclone) with the `drive` provider.

### Microsoft OneDrive

**With the OneDrive app:**

1. Windows has it built in. On a Mac, install OneDrive from the App Store. Sign in.
2. In Claude Backup, choose OneDrive under **Found on this computer**, or choose **Microsoft OneDrive** and pick your OneDrive folder.
   - On a Mac it's in `~/Library/CloudStorage/OneDrive-<name>`.
   - On Windows it's in `%UserProfile%\OneDrive`.
3. On a Mac, if Files On-Demand is on, right-click the `Claude Backups` folder and choose **Always Keep on This Device**, so restores don't wait for downloads.

**Without the app, or for OneDrive for Business and SharePoint:** use [rclone](#any-other-cloud-with-rclone) with the `onedrive` provider.

### Dropbox, iCloud Drive and Box

Install the provider's desktop app (Dropbox, Box Drive), or turn on iCloud Drive (System Settings → Apple Account → iCloud). Then choose the folder under **Found on this computer**, or pick the matching type and choose the folder.

### Amazon S3

1. In the AWS console, create a bucket. Keep **Block all public access** on. Turning on versioning is a good idea.
2. Create an IAM user (or role) with this policy, replacing `my-claude-backups`:

```
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::my-claude-backups"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::my-claude-backups/*"}
  ]
}
```

3. Create an access key for that user.
4. In Claude Backup, choose **Amazon S3 or S3-compatible** and enter:
   - **Bucket** and **Folder in bucket** (for example `claude-backups`)
   - **Region**, for example `us-east-1`. It must be the bucket's region.
   - **Sign in with:** either an **Access key** (ID and secret; the secret is stored in your Keychain), or an **AWS profile** from `~/.aws/credentials`. Profiles that use AWS SSO aren't supported; use an access key or rclone.
5. Click **Save and test**.

Uploads are signed with AWS Signature Version 4 over HTTPS.

### S3-compatible storage (Backblaze B2, Wasabi, Cloudflare R2, MinIO, DigitalOcean Spaces)

Follow the Amazon S3 steps, using the provider's access keys, and set **Endpoint**:

| Provider | Endpoint | Region |
|---|---|---|
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` (shown on the bucket page) | the region in the endpoint, for example `us-west-004` |
| Wasabi | `https://s3.<region>.wasabisys.com` | for example `us-east-1` |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` |
| MinIO | your server, for example `http://localhost:9000` | `us-east-1` |
| DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | for example `nyc3` |

### Microsoft Azure Blob Storage

1. In the Azure portal, create (or open) a **Storage account**, then a **Container**. Keep its access level **Private**.
2. Open the container, then **Shared access tokens**.
3. Under **Permissions**, tick **Read, Add, Create, Write, Delete, List**. Set an expiry date, for example a year from now, and note it down.
4. Click **Generate SAS token and URL** and copy the **Blob SAS URL**.
5. In Claude Backup, choose **Microsoft Azure Blob Storage**, paste the URL, and set **Folder in container**.
6. Click **Save and test**.

The SAS token is stored in your Keychain. When it expires, edit the destination and paste a new URL. Files of 64 MB or more are uploaded in 8 MB blocks.

### Any other cloud with rclone

[rclone](https://rclone.org) is a free tool that connects to 70+ storage providers directly, including Google Drive, OneDrive (personal, Business, SharePoint), Dropbox, Box, pCloud, Mega, Proton Drive, SFTP, WebDAV and Nextcloud.

1. Install rclone. On a Mac with Homebrew: `brew install rclone`. On Windows: `winget install Rclone.Rclone`. Otherwise see https://rclone.org/install/.
2. Set up a remote. In Claude Backup, choose **Other cloud via rclone** and click **Set up a new remote…**, which opens a Terminal. Or run `rclone config` yourself:
   - Type `n` for a new remote and give it a short name, for example `gdrive`.
   - Pick the storage type, for example `drive` for Google Drive or `onedrive` for OneDrive.
   - Accept the defaults. When asked, let rclone open your browser and sign in.
3. Back in Claude Backup, click **Refresh**. Enter **Remote and folder** as `name:folder`, for example `gdrive:Claude Backups`.
4. Click **Save and test**.

### Restore from a drive or cloud

1. Open **Cloud & drives** and click **Browse** next to the destination.
2. Click **Download & restore**. The backup is downloaded into your backup folder and opened on the **Restore** screen.

From the command line: `claude-backup remote <destination>`, then `claude-backup download <destination> <file name>`.

## Automatic backups

Open **Automatic** to back up on a schedule, even when the app is closed:

1. Choose **Every day** or **Every week on** a day, and a time.
2. Choose the categories and accounts.
3. Set **Keep newest**: older backups in the folder go to the Trash. This applies to every backup in that folder, so keep any you want to protect in another folder.
4. Click **Save schedule**.

Scheduled backups are also copied to every destination marked **Copy every new backup here**. **Run a backup now** makes one immediately with the same settings, and **Show log** shows the scheduled runs.

The schedule uses your system's own scheduler: a LaunchAgent on macOS (`~/Library/LaunchAgents/com.claudebackup.auto.plist`), Task Scheduler on Windows (task `ClaudeBackupAuto`), and cron on Linux. Your computer must be awake. On macOS, a missed run happens when it wakes.

## Library

**Library** lists every backup in your backup folder. Click one to:

- browse its contents by category and item, and preview any file
- **Verify** it: every file's checksum is checked
- **Extract selected…** to copy items out as plain files without restoring them
- **Copy to…** a drive or cloud
- **Restore…**, **Show file**, or **Move to Trash**

**Add backup file…** copies a backup from elsewhere (a USB stick or email attachment) into your library.

## Artifacts screen

1. Choose your claude.ai export zip and click **Load**.
2. Search by title or conversation. Click a row to preview it.
3. Then:
   - **View in browser** opens the artifact in a sandboxed tab, so it can't reach the app.
   - **Open with app** uses your default program.
   - **Save selected…** and **Save all…** save files into a folder of your choice.
   - **Include in a backup** goes to Back up with the export already chosen.

## Undo a restore

Before every restore, the files it will change are copied into a snapshot. In **Undo & activity**:

- **Undo** puts every changed file back and removes files the restore created.
- **What it changed** lists the files.
- **Delete** removes a snapshot you no longer need.

The **Activity** log below lists everything the app has done.

## Settings and where files live

**Settings** has your backup folder, default skill destinations, the default for removing API keys, where Claude keeps its data, the Python version in use, and **Quit Claude Backup**.

| What | Where |
|---|---|
| Backups | `~/ClaudeBackups` (change it in Settings) |
| Upload zips and exported files from restores | `~/ClaudeBackups/restored-<date>/` |
| Undo snapshots | `~/ClaudeBackups/snapshots/` |
| App settings, destinations, activity log, app log | `~/.claude-backup/` |
| Secrets (keys, SAS tokens) | macOS Keychain (service `claude-backup`); elsewhere `~/.claude-backup/secrets.json` |
| Claude Code data | `~/.claude/` and `~/.claude.json` |
| Claude Desktop data | macOS: `~/Library/Application Support/Claude`; Windows: `%APPDATA%\Claude`; Linux: `~/.config/Claude` |

## Command line

Run these from the project folder with `python3 -m claude_backup ...`, or with `claude-backup ...` if you installed with pip.

```
claude-backup                         open the app in your browser
claude-backup scan                    what can be backed up
claude-backup accounts                Claude accounts with data on this computer
claude-backup backup                  full backup (also copied to "every backup" destinations)
claude-backup backup -c skills,prompts --no-upload
claude-backup backup --export ~/Downloads/claude-export.zip --redact-secrets
claude-backup backup --to "My S3" --keep 10
claude-backup inspect FILE            what a backup contains
claude-backup verify FILE             check every checksum
claude-backup restore FILE --dry-run
claude-backup restore FILE -c skills --skills claude-code,upload,desktop
claude-backup restore FILE --to-account UUID --to-org UUID --skip-existing
claude-backup migrate --from-account A --from-org O1 --to-account B --to-org O2
claude-backup undo ~/ClaudeBackups/snapshots/pre-restore-....zip
claude-backup list                    backups and undo snapshots
claude-backup destinations --detect   your destinations, and drives/clouds found here
claude-backup upload FILE --to NAME   copy a backup to a destination
claude-backup remote NAME             backups stored in a destination
claude-backup download NAME FILENAME  download one into your backup folder
claude-backup make-app                build Claude Backup.app (macOS)
```

Exit codes: `0` success, `2` error, `3` backup made but a copy to a destination failed.

## Security and privacy

- Everything runs on your computer. Nothing leaves it except copies you send to destinations you add.
- The app's page and server listen on `127.0.0.1` only. Every request needs a random key created at launch, which only the page you opened knows.
- Backups are **not encrypted**. They can contain MCP API keys (unless you tick **Remove API keys and tokens**), transcripts of your Claude Code sessions, and your prompts. Treat them like your documents:
  - Keep cloud buckets and containers private.
  - Prefer providers that encrypt at rest (S3, Azure, Google Drive and OneDrive all do).
  - Give access keys only the permissions listed above.
- Sign-in tokens and credentials are never backed up, and a restore never changes who is signed in.
- Cloud secrets are kept in the macOS Keychain, or in a file readable only by you.
- Artifacts open in a sandboxed browser tab that can't talk to the app.

## Troubleshooting

**"Claude Desktop is running."** Quit it (Claude → Quit) before restoring, otherwise it may overwrite restored files when it closes.

**My skills don't show up in the new account.** Upload the zips from `skills-to-upload` (see [Upload skills and plugins to an account](#upload-skills-and-plugins-to-an-account)), then restart Claude Desktop.

**A category shows 0 items.** Claude's local storage layout isn't a published interface, and it can change between Claude versions. Check that Claude Desktop or Claude Code has been used on this computer, and see **Settings → Where Claude keeps your data**. Please open an issue if a newer Claude version moved things.

**"No signed-in account found."** Sign in to Claude Desktop, or run `claude` and `/login`, then click **Refresh**. You can also choose **Another account (enter its IDs)** on the Restore screen.

**A drive or cloud test fails.**
- External drive or synced folder: is it plugged in, and is the cloud app running and signed in?
- S3: `AccessDenied` means the key lacks permissions. `PermanentRedirect` or `AuthorizationHeaderMalformed` means the region is wrong. `SignatureDoesNotMatch` means the secret key is wrong.
- Azure: `AuthenticationFailed` usually means the SAS token expired. Generate a new one with all six permissions.
- rclone: run `rclone lsd name:` in a terminal to check the remote. Run `rclone config reconnect name:` if its sign-in expired.

**The app doesn't open.** Run `python3 -m claude_backup` in a terminal to see errors, and check `~/.claude-backup/app.log`. If a page says "Open Claude Backup from its app icon", the app was restarted: open it again from its icon.

**The page says the app stopped responding.** It stopped after being idle. Open it again.

**Automatic backups don't run.** Check **Automatic → Show log**. On macOS, make sure the project folder hasn't moved; if it has, open **Automatic** and click **Save schedule** again.

## Uninstall

1. **Automatic → Turn off**, and **Settings → Quit Claude Backup**.
2. Delete the project folder and `Claude Backup.app`.
3. Optionally delete `~/.claude-backup`, your backups in `~/ClaudeBackups`, and any Keychain items named "Claude Backup: …" (Keychain Access → search "claude-backup").
