#!/usr/bin/env python3
"""Import previously collected Telegram user IDs into the bot's SQLite database.

Usage:
  python import_legacy_users.py legacy_user_ids.txt

Input format: one numeric Telegram user ID per line. Blank lines and lines
starting with # are ignored. This does NOT discover historical users; it only
imports IDs you already have permission to use.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent / "data"))).expanduser()
DB_PATH = DATA_DIR / "baby5_admin.sqlite3"

def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python import_legacy_users.py legacy_user_ids.txt")
        return 2
    source = Path(sys.argv[1])
    if not source.is_file():
        print(f"File not found: {source}")
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    imported = 0
    skipped = 0
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                starts INTEGER NOT NULL DEFAULT 1,
                start_logged INTEGER NOT NULL DEFAULT 0,
                translations INTEGER NOT NULL DEFAULT 0,
                last_action TEXT DEFAULT ''
            )
        """)
        for line in source.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                user_id = int(value)
                if user_id <= 0:
                    raise ValueError
            except ValueError:
                skipped += 1
                continue
            cur = db.execute(
                """INSERT OR IGNORE INTO users
                   (user_id, username, full_name, first_seen, last_seen)
                   VALUES (?, '', '', ?, ?)""",
                (user_id, now, now),
            )
            if cur.rowcount:
                imported += 1
            else:
                skipped += 1
    print(f"Import complete. Added: {imported}; already present/invalid: {skipped}")
    print(f"Database: {DB_PATH}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
