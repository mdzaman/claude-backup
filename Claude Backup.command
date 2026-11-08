#!/bin/bash
# Double-click on macOS to open Claude Backup.
cd "$(dirname "$0")" && exec python3 -m claude_backup ui
