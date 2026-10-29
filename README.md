# Baby5 Sinhala Subtitle Bot — Fixed V10

A Telegram subtitle bot that translates SRT, ASS, and SSA subtitles from any source language into natural spoken Sri Lankan Sinhala. ASS/SSA files are parsed internally, translated using the same pipeline as SRT, and delivered as translated SRT outputs.

## User commands
- `/start` — welcome message and upload instructions.
- `/help` — usage guide.
- `/status` — current queue, job, model, and batch size.
- `/settings` — choose your own batch size (30/50/100 lines) and translation mode (Normal/Fast/Super Fast).

Admin-only commands:
- `/change_model` — choose a Gemini model.
- `/test_gemini` — test the Gemini connection.
- `/reset` — clear active state and temporary files.
- `/stop` — stop the active job for everyone; the current Gemini request is allowed to finish first.

## Queue and output behavior
- Upload up to 12 SRT/ASS/SSA files, or a ZIP containing up to 12 subtitle files.
- The queue updates in one message. The blue **▶️ Start Translation** button starts the job; the red **❌ Cancel Queue** button clears an unstarted queue.
- An unstarted queue is cleared after 3 minutes without activity.
- Each finished file is sent immediately; processing does not wait for all files in the queue.
- At the end, one ZIP containing all outputs from that queue is sent automatically.
- The compact mobile-friendly live progress message shows a styled progress bar, current file index, processed-line count, successfully translated-line count, translation mode, batch size, model, queue size, errors, and a red **🔴 Stop** button. The percentage measures processed lines; the Sinhala counter measures successful translations only.
- ASS/SSA files are parsed internally into SRT-compatible subtitle entries, then sent through the same Gemini translation pipeline as SRT files. The user receives translated SRT output, never conversion-only output.

## Sinhala output quality
- The prompt explicitly requires Sinhala Unicode script, not romanized Sinhala/Singlish.
- A validation check rejects likely romanized-Sinhala or Tamil-script outputs so they can be retried/split instead of silently accepted. Quota errors rotate keys without retrying the same exhausted key, and all-key quota exhaustion stops cleanly rather than triggering recursive batch splitting across every model. Persistent 503 overloads use bounded retries and stop rather than multiplying requests.
- English/Latin text is allowed for real names, brands, titles, and recognizable source terms such as Netflix, YouTube, Cola, and established technique names.
- Subtitle IDs and original timestamps are preserved for existing cues.
- Translated SRT files receive a brief opening and closing credit cue: `Translate by @Baby_5_advancetr_bot`. Subtitle-player support for SRT font-color tags varies.

## Colab setup
Use the included `Anime_Sinhala_Bot_V10_Colab_Guide.txt`. Add `BOT_TOKEN`, `ADMIN_USER_ID`, and `GEMINI_API_KEY_1` to Colab Secrets. `GEMINI_API_KEY_2` through `_10` are optional. The bot rotates across up to 10 API keys for the selected model and does not automatically switch models (AUTO_MODEL_FALLBACK=false).

## Notes
- Telegram button color styling requires a recent Telegram client; older clients may display buttons without the requested colors.
- One bot token should be polled by only one running bot instance at a time. Stop any old runtime/container before starting a second instance.


## Admin panel and developer logs
- `/admin` is owner-only and manages additional admin accounts.
- `/panel` is available to the owner and promoted admins. It includes ban/unban, users settings, statistics, maintenance mode, and broadcast.
- `/broadcast` supports text and Telegram media/stickers, and asks for confirmation before sending.
- VIP users and users granted **Remove Limit** are exempt from the normal daily file limit. The default limit is unlimited until an admin sets one.
- Per-user batch size and translation mode are saved independently, so one user's `/settings` choices do not change another user's settings.
- Set `DEV_GROUP_ID` in Railway Variables to the numeric ID of the private developer group. Add the bot to that group and allow it to send messages/files. Logs include first `/start` only once per user, user-uploaded supported files, translated output files, translation failures, and actions performed by promoted admins. Actions/files belonging to the configured owner (`ADMIN_USER_ID`) are excluded from developer logs.
- Admin lists, bans, VIP/unlimited grants, user records, daily usage, and settings are stored in `DATA_DIR/baby5_admin.sqlite3`. On Railway, attach a persistent volume mounted at `/app/data` (or set `DATA_DIR` to the volume mount path) so these survive redeploys.


## API key retry and translation modes
- Only the selected Gemini model is used; there is no automatic model fallback.
- Normal mode sends one batch at a time. A failed batch tries each configured key in order, with 2 attempts per key, for up to 10 complete rounds. A successful key is reused for the next batch.
- Fast mode sends two fixed batches concurrently. The first lane uses key 1 and retries through keys 3, 5, 7, 9; the second lane uses key 2 and retries through keys 4, 6, 8, 10.
- Super Fast mode sends up to five fixed batches concurrently. The lanes start with keys 1–5 and retry through keys 6–10 respectively.
- Batch-to-lane assignments do not change while retrying; a later batch cannot take over a failed batch's lane.
- Each new job starts with key 1 and a fresh round counter. Stopping or finishing a job resets rotation for the next job.
- Translation progress is updated as each parallel batch completes, based on the number of subtitle lines completed.


## Oracle VPS persistent database

See `ORACLE_VPS_DATABASE_GUIDE.md` for persistent SQLite storage, safe updates, and the limits of recovering old Render user IDs. The bot already stores `/start` users in SQLite under `DATA_DIR`; on Oracle, keep `DATA_DIR` pointed at a persistent directory such as `/home/ubuntu/anime-bot-data`.
