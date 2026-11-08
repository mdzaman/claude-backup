# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's **Report a vulnerability** button (Security tab → Advisories) to send a private report. Include the steps to reproduce it, and which version or commit you used.

## Design notes

- The app listens on `127.0.0.1` only. Every API request needs a random token generated at launch.
- Artifacts are viewed from a sandboxed origin (`Content-Security-Policy: sandbox`), so they can't call the API.
- Sign-in tokens, `.credentials.json`, cookies and device keys are never collected.
- Destination secrets are stored in the macOS Keychain. Elsewhere they're stored in `~/.claude-backup/secrets.json` with mode `0600`, never in the settings file.
- Backups are not encrypted. Users are told to keep cloud storage private, and can remove API keys from MCP settings before backing up.
- Destinations only ever list or delete files named `claude-backup-*.claudebackup`.
