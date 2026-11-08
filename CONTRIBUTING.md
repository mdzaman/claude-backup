# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Ground rules

- **No third-party dependencies.** Claude Backup runs on a stock Python 3.8+ install. Use only the standard library.
- **Never touch real user data in tests.** Tests build fake Claude installs in temporary folders (see `tests/test_engine.py`, `make_home`).
- **Never back up secrets.** Sign-in tokens, `.credentials.json`, cookies and device keys must stay excluded. Add a test if you add a new source.
- **Restores must be undoable.** Any new restore operation has to record what it changes in the snapshot.

## Setup

```bash
git clone https://github.com/mdzaman/claude-backup.git
cd claude-backup
python3 -m claude_backup          # run the app
python3 -m unittest discover -s tests -v
```

## Where things are

| Change | File |
|---|---|
| What gets collected, backup format, restore logic | `claude_backup/engine.py` |
| Where Claude keeps data on each OS | `claude_backup/env.py` |
| A new cloud or drive type | `claude_backup/destinations.py` (add a class with `test`, `upload`, `list`, `download`, `delete`, and register it in `TYPES` and `KINDS`) |
| The browser app | `claude_backup/web.py` (API) and `claude_backup/ui.html` (page) |
| Documentation | `claude_backup/docs/USER_GUIDE.md` (also shown in the app's Help screen) and `README.md` |

## Claude changed where it stores something

Claude's local storage layout isn't a published interface. If a Claude update moves things:

1. Open an issue with your OS, Claude Desktop or Claude Code version, and the output of `python3 -m claude_backup scan`. Don't paste file contents or anything with your account details.
2. Update the paths in `env.py` or the collectors in `engine.py`, and extend `make_home` in the tests to cover the new layout.

## Pull requests

- Keep changes focused, and add or update tests.
- Update `USER_GUIDE.md` and `CHANGELOG.md` for anything users will notice.
- By contributing, you agree that your contributions are licensed under the Apache License 2.0.
