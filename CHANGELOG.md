# Changelog

## 1.2.0

- Fully responsive interface for phones, tablets and desktops, with bottom navigation, card-style tables and larger touch targets.
- Phone access for iPhone and Android: QR pairing over HTTPS, home-screen install, and a restricted set of actions. Off by default.

## 1.1.0

- Cloud and drive destinations: external drives; synced folders (Google Drive, OneDrive, Dropbox, iCloud Drive, Box); Amazon S3 and S3-compatible storage (SigV4, no SDK needed); Azure Blob Storage (SAS); any rclone remote.
- Copy every backup to chosen destinations automatically, including scheduled and command-line backups; keep the newest N per destination.
- Browse, download, download-and-restore, and delete backups in any destination.
- Secrets are stored in the macOS Keychain.
- In-app Help screen with the full user guide.
- New commands: `destinations`, `upload`, `remote`, `download`; `backup --to/--no-upload`.

## 1.0.0

- Browser app: dashboard, back up, restore, migration wizard, library, artifacts, automatic backups, undo and activity, settings.
- Backup engine with account-ID rewriting, merged config restores, undo snapshots, checksums and secret exclusion.
- claude.ai export support: artifacts at their final version, project instructions and knowledge, prompt history.
- macOS app bundle, Windows launcher, command line.
