# Oracle VPS deployment and persistent Telegram user database

## Important: what this database can and cannot recover

The bot already uses SQLite (`data/baby5_admin.sqlite3`) to store users and admin/settings data. Every user who sends `/start` to this running bot is recorded. Keep `DATA_DIR` on the VPS's persistent local disk, outside any temporary build/container directory, and do not delete that folder when updating code.

**A new Oracle deployment starts with a new empty database.** Telegram does not provide a bot with a list of everyone who ever used it. If the old Render deployment truly did not store IDs, those old IDs cannot be reconstructed automatically. You can only import IDs that you already have from logs, a backup, or another legitimate source. Users who start the bot after the Oracle deployment will be saved going forward.

## Recommended directory layout on Oracle

Keep code and persistent data separate:

- Code: `/home/ubuntu/anime-sinhala-bot`
- Database/data: `/home/ubuntu/anime-bot-data`

Set this environment variable when running the bot:

```bash
export DATA_DIR=/home/ubuntu/anime-bot-data
```

The folder must be writable by the `ubuntu` user. This is persistent on the VM's boot disk across process restarts and code updates, but not if you delete the VM/boot disk. Keep backups.

## Updating later without losing user IDs

1. Stop the bot process/service.
2. Update the code in the code directory (for example, `git pull`).
3. Keep `DATA_DIR=/home/ubuntu/anime-bot-data` unchanged.
4. Start the bot again.

Do not delete `/home/ubuntu/anime-bot-data/baby5_admin.sqlite3`. Back it up before major updates:

```bash
mkdir -p /home/ubuntu/backups
cp /home/ubuntu/anime-bot-data/baby5_admin.sqlite3 \
  "/home/ubuntu/backups/baby5_admin-$(date +%F-%H%M%S).sqlite3"
```

## Importing old IDs if you recover them

Create a text file with one numeric Telegram user ID per line, e.g. `legacy_user_ids.txt`. Do not include bot tokens or passwords. Copy the file to the VPS, then from the project directory run:

```bash
python import_legacy_users.py legacy_user_ids.txt
```

This imports IDs into the SQLite `users` table so the bot's existing broadcast feature can target them. The script does not discover IDs and cannot make Telegram deliver messages to users who have blocked the bot. Use IDs only if you already have a legitimate record of those users.

## Secrets

Do not commit `.env`, Telegram bot tokens, Gemini API keys, SSH private keys, or passwords to GitHub. Set these as environment variables on the VPS instead. Keep the repository private as an additional precaution, not as a replacement for secret management.

## One polling instance

Before starting the bot on Oracle, stop the Render service using the same Telegram bot token. Telegram polling should not be active on both deployments at once.
