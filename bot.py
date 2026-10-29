from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sqlite3
import time
from datetime import date
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, BotCommandScopeAllPrivateChats,
    InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update,
)

# Telegram Bot API supports actual inline-button styles: primary (blue),
# success (green), and danger (red). Use api_kwargs for forward compatibility
# with python-telegram-bot versions that do not expose `style` as a named arg.
_TelegramInlineKeyboardButton = InlineKeyboardButton
def InlineKeyboardButton(text, *args, **kwargs):
    callback_data = kwargs.get("callback_data")
    if callback_data is not None and "style" not in kwargs:
        label = str(text).lower()
        danger_terms = ("stop", "cancel", "delete", "remove", "ban", "danger", "reset")
        success_terms = ("confirm", "add ", "unban", "turn on", "enable", "done", "statistics")
        if any(term in label for term in danger_terms) or any(term in str(callback_data).lower() for term in ("cancel", "remove", "delete", "ban:add", "stop_job")):
            style = "danger"
        elif any(term in label for term in success_terms) or label.startswith(("🟢", "✅", "📊")):
            style = "success"
        else:
            style = "primary"
        api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
        api_kwargs["style"] = style
        kwargs["api_kwargs"] = api_kwargs
    return _TelegramInlineKeyboardButton(text, *args, **kwargs)

from google.genai import types
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from config import Settings, MODEL_LABELS
from gemini_translator import GeminiPool, GeminiQuotaExhaustedError, GeminiServiceUnavailableError, GeminiBatchFailedError
from subtitle_utils import (
    SubtitleItem,
    ass_to_srt,
    build_chunks,
    detect_context_hint,
    extract_zip,
    is_subtitle_name,
    make_output_path,
    read_srt,
    safe_name,
    unique_path,
    validate_file_size,
    write_srt,
    format_srt_time,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PendingFile:
    path: Path
    original_name: str
    source_label: str | None = None


@dataclass
class LastResults:
    output_paths: list[Path]
    base_dir: Path
    chat_id: int
    created_at: float
    release_task: asyncio.Task | None = None


@dataclass
class BotState:
    owner_id: int | None = None
    owner_name: str = ""
    pending: list[PendingFile] = field(default_factory=list)
    last_results: LastResults | None = None
    selected_model: str = ""
    processing: bool = False
    cancel_requested: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    work_dir: Path | None = None
    progress_message_id: int | None = None
    queue_message_id: int | None = None
    queue_chat_id: int | None = None
    error_messages: list[str] = field(default_factory=list)
    translated_successes: int = 0
    failed_subtitles: int = 0
    batch_size: int = 50
    queue_timeout_task: asyncio.Task | None = None
    _last_progress_text: str = ""


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class SubtitleBot:

    def __init__(self, settings: Settings):
        self.s = settings
        self.state = BotState(selected_model=settings.default_model)
        self.gemini = GeminiPool(
            settings.gemini_keys,
            settings.default_model,
            settings.models,
            settings.auto_model_fallback,
        )
        self.gemini.base_inter_call_delay = settings.inter_call_delay
        self.work_root = settings.data_dir / "jobs"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.application: Application | None = None
        self.state_lock = asyncio.Lock()
        self.db_path = settings.data_dir / "baby5_admin.sqlite3"
        self._init_db()

    # ------------------------------------------------------------------
    # Tiny helpers
    # ------------------------------------------------------------------

    def is_owner(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u and u.id == self.s.admin_user_id)

    def is_admin(self, update: Update) -> bool:
        u = update.effective_user
        if not u:
            return False
        if u.id == self.s.admin_user_id:
            return True
        return self._db_has("admins", u.id) and not self._db_has("banned", u.id)

    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, starts INTEGER NOT NULL DEFAULT 1,
                    start_logged INTEGER NOT NULL DEFAULT 0,
                    translations INTEGER NOT NULL DEFAULT 0, last_action TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, added_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS banned(user_id INTEGER PRIMARY KEY, reason TEXT DEFAULT '', added_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vip(user_id INTEGER PRIMARY KEY, added_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS unlimited(user_id INTEGER PRIMARY KEY, added_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage(user_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, day));
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS user_settings(user_id INTEGER PRIMARY KEY, batch_size INTEGER NOT NULL DEFAULT 50, daily_limit INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'normal');
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(users)")}
            if "start_logged" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN start_logged INTEGER NOT NULL DEFAULT 0")
            user_settings_columns = {r[1] for r in db.execute("PRAGMA table_info(user_settings)")}
            if "mode" not in user_settings_columns:
                db.execute("ALTER TABLE user_settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal'")

    def _db_has(self, table: str, user_id: int) -> bool:
        if table not in {"admins", "banned", "vip", "unlimited"}:
            return False
        with self._db() as db:
            return db.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (int(user_id),)).fetchone() is not None

    def _user_record(self, user) -> bool:
        """Upsert user metadata; return True only on first-ever observation."""
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with self._db() as db:
            row = db.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
            if row:
                db.execute("UPDATE users SET username=?, full_name=?, last_seen=? WHERE user_id=?",
                           (user.username or "", user.full_name or "", now, user.id))
                return False
            db.execute("INSERT INTO users(user_id,username,full_name,first_seen,last_seen) VALUES(?,?,?,?,?)",
                       (user.id, user.username or "", user.full_name or "", now, now))
            db.execute("INSERT OR IGNORE INTO user_settings(user_id,batch_size,daily_limit) VALUES(?,?,0)", (user.id, 50))
            return True

    def _mark_start_once(self, user_id: int) -> bool:
        with self._db() as db:
            row = db.execute("SELECT start_logged FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row or int(row[0]) == 1:
                return False
            db.execute("UPDATE users SET start_logged=1, starts=1 WHERE user_id=?", (user_id,))
            return True

    def _set_user_action(self, user_id: int, action: str) -> None:
        with self._db() as db:
            db.execute("UPDATE users SET last_action=?, last_seen=? WHERE user_id=?",
                       (action[:120], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), user_id))

    def _get_batch(self, user_id: int) -> int:
        with self._db() as db:
            row = db.execute("SELECT batch_size FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return int(row[0]) if row else 50

    def _set_batch(self, user_id: int, size: int) -> None:
        with self._db() as db:
            db.execute("INSERT INTO user_settings(user_id,batch_size,daily_limit) VALUES(?,?,0) ON CONFLICT(user_id) DO UPDATE SET batch_size=excluded.batch_size",
                       (user_id, size))

    def _get_mode(self, user_id: int) -> str:
        with self._db() as db:
            row = db.execute("SELECT mode FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            return str(row[0]) if row and row[0] in {"normal", "fast", "superfast"} else "normal"

    def _set_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"normal", "fast", "superfast"}:
            return
        with self._db() as db:
            db.execute("INSERT INTO user_settings(user_id,batch_size,daily_limit,mode) VALUES(?,?,0,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode", (user_id, 50, mode))

    def _is_premium_mode_user(self, user_id: int) -> bool:
        return user_id == self.s.admin_user_id or self._db_has("vip", user_id)

    def _mode_enabled(self, mode: str) -> bool:
        if mode not in {"fast", "superfast"}:
            return True
        key = "fast_mode_enabled" if mode == "fast" else "superfast_mode_enabled"
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row is None or row[0] == "1"

    async def _dev_log(self, text: str, *, file_path: Path | None = None, source_chat_id: int | None = None, source_message_id: int | None = None) -> None:
        """Developer logs intentionally exclude every action performed by the configured owner."""
        if not self.s.dev_group_id or not self.application:
            return
        try:
            if text:
                await self.application.bot.send_message(chat_id=self.s.dev_group_id, text=text[:3900])
            if file_path and file_path.exists() and file_path.stat().st_size <= 50 * 1024 * 1024:
                with file_path.open("rb") as fh:
                    await self.application.bot.send_document(chat_id=self.s.dev_group_id, document=InputFile(fh, filename=file_path.name), caption=(text[:900] if text else file_path.name))
            elif source_chat_id and source_message_id:
                await self.application.bot.copy_message(chat_id=self.s.dev_group_id, from_chat_id=source_chat_id, message_id=source_message_id)
        except Exception as exc:
            print(f"[DEV_GROUP_ID log error] {exc.__class__.__name__}: {str(exc)[:160]}")

    async def _notify_user(self, user_id: int, text: str) -> None:
        """Send an English-only account/permission notice; ignore users who blocked the bot."""
        try:
            await self.application.bot.send_message(chat_id=int(user_id), text=text)
        except Exception as exc:
            print(f"[user notification] ID {user_id}: {exc.__class__.__name__}")

    async def _log_admin_action(self, update: Update, action: str) -> None:
        u = update.effective_user
        if not u or u.id == self.s.admin_user_id:
            return
        await self._dev_log(f"🛡️ ADMIN ACTION\nAdmin: {u.full_name}\nUsername: @{u.username or 'N/A'}\nUser ID: {u.id}\nAction: {action}\nTime (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")

    def _is_banned(self, user_id: int) -> bool:
        return self._db_has("banned", user_id)

    def _daily_limit(self, user_id: int) -> int:
        if user_id == self.s.admin_user_id or self._db_has("vip", user_id) or self._db_has("unlimited", user_id):
            return 0
        with self._db() as db:
            row = db.execute("SELECT daily_limit FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
            if row and int(row[0]) > 0:
                return int(row[0])
            row = db.execute("SELECT value FROM settings WHERE key='daily_limit' ").fetchone()
            return int(row[0]) if row else 0

    def _usage_today(self, user_id: int) -> int:
        with self._db() as db:
            row = db.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, date.today().isoformat())).fetchone()
            return int(row[0]) if row else 0

    def _increment_usage(self, user_id: int, amount: int = 1) -> None:
        with self._db() as db:
            db.execute("INSERT INTO usage(user_id,day,count) VALUES(?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET count=count+excluded.count",
                       (user_id, date.today().isoformat(), amount))
            db.execute("UPDATE users SET translations=translations+?, last_action='translation completed' WHERE user_id=?", (amount, user_id))

    def _registered_user_ids(self) -> list[int]:
        with self._db() as db:
            return [int(r[0]) for r in db.execute("SELECT user_id FROM users ORDER BY first_seen")]

    def _display_user(self, row) -> str:
        uname = f"@{row['username']}" if row['username'] else "no username"
        return f"{row['full_name'] or 'Unknown'} ({uname}) — ID {row['user_id']}"

    def _message(self, update: Update):
        return update.effective_message

    def _is_other_user(self, user_id: int) -> bool:
        return self.state.owner_id is not None and self.state.owner_id != user_id

    def _claim_owner(self, user_id: int, name: str = "") -> None:
        if self.state.owner_id is None:
            self.state.owner_id = user_id
            self.state.owner_name = name[:80]

    def _ensure_work_dir(self) -> Path:
        if self.state.work_dir and self.state.work_dir.exists():
            return self.state.work_dir
        job_dir = self.work_root / f"job_{int(time.time() * 1000)}"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "incoming").mkdir(exist_ok=True)
        (job_dir / "converted").mkdir(exist_ok=True)
        (job_dir / "outputs").mkdir(exist_ok=True)
        self.state.work_dir = job_dir
        return job_dir

    def _clear_release_timer(self) -> None:
        if self.state.last_results and self.state.last_results.release_task:
            self.state.last_results.release_task.cancel()
            self.state.last_results.release_task = None

    def _cleanup_job_dir(self) -> None:
        if self.state.work_dir:
            shutil.rmtree(self.state.work_dir, ignore_errors=True)
        self.state.work_dir = None

    def _hard_clear_state(self, keep_model: bool = False) -> None:
        qt = self.state.queue_timeout_task
        if qt and qt is not asyncio.current_task() and not qt.done():
            qt.cancel()
        selected = self.state.selected_model
        batch = self.state.batch_size
        self._clear_release_timer()
        self.state = BotState(
            selected_model=selected if keep_model else self.s.default_model,
            batch_size=batch,
        )
        self.gemini.set_model(self.state.selected_model)

    @staticmethod
    def _short_error(exc: Exception) -> str:
        text = " ".join(str(exc).split()).replace("GEMINI_API_KEY", "API_KEY")
        return text[:180] or exc.__class__.__name__

    def _format_errors(self) -> str:
        msgs = self.state.error_messages
        if not msgs:
            return ""
        shown = msgs[-self.s.max_errors_shown:]
        text = "⚠️ Small issues:\n" + "\n".join(f"• {x}" for x in shown)
        if len(msgs) > len(shown):
            text += f"\n• … +{len(msgs) - len(shown)} more"
        return text

    # ------------------------------------------------------------------
    # Queue expiry
    # ------------------------------------------------------------------

    def _reset_queue_timeout(self, chat_id: int) -> None:
        t = self.state.queue_timeout_task
        if t and t is not asyncio.current_task() and not t.done():
            t.cancel()
        self.state.queue_timeout_task = asyncio.create_task(
            self._expire_idle_queue(chat_id)
        )

    async def _expire_idle_queue(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(180)
            if self.state.pending and not self.state.processing and self.state.queue_chat_id == chat_id:
                count = len(self.state.pending)
                await self._delete_queue_message()
                self._cleanup_job_dir()
                self._hard_clear_state(keep_model=True)
                with contextlib.suppress(Exception):
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⌛ Queue expired after 3 minutes of inactivity. "
                            f"{count} queued file(s) were cleared. Upload your files again when ready."
                        ),
                    )
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Stop helpers (shared by button + admin /stop)
    # ------------------------------------------------------------------

    async def _signal_stop(self) -> None:
        """Set cancel flags and disable the in-progress Stop button immediately."""
        self.state.cancel_requested = True
        self.state.cancel_event.set()

        pid = self.state.progress_message_id
        cid = self.state.queue_chat_id
        if pid and cid and self.application:
            with contextlib.suppress(Exception):
                await self.application.bot.edit_message_reply_markup(
                    chat_id=cid,
                    message_id=pid,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⏳ Stopping…", callback_data="noop")
                    ]]),
                )

    async def _wait_for_job_finish(self, timeout: float = 10.0) -> None:
        """Wait up to `timeout` seconds for the active job task to complete."""
        t = self.state.task
        if t and not t.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout=timeout)

    # ------------------------------------------------------------------
    # Commands — public
    # ------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = self._message(update)
        if not user or not message:
            return
        self._user_record(user)
        with contextlib.suppress(Exception):
            await self._refresh_user_commands(user.id)
        if self._is_banned(user.id):
            await message.reply_text("Access denied.")
            return
        first_start = self._mark_start_once(user.id)
        if first_start and user.id != self.s.admin_user_id:
            await self._dev_log(f"🆕 NEW USER STARTED\nName: {user.full_name}\nUsername: @{user.username or 'N/A'}\nUser ID: {user.id}\nFirst seen (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
        with self._db() as db:
            maintenance = db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
        if maintenance and maintenance[0] == '1' and not self.is_admin(update):
            await message.reply_text("The bot is temporarily under maintenance. Please try again later.")
            return
        self._set_user_action(user.id, "/start")
        if self._is_other_user(user.id):
            await self._busy_message(update)
            return
        if self.state.processing:
            await message.reply_text("⏳ A subtitle job is currently running. Please wait until it finishes.")
            return
        await message.reply_text(
            "✨ *Welcome to Sinhala Subtitle Bot!*\n\n"
            "🎬 Translate subtitles from any language into natural Sinhala.\n"
            "📂 Upload SRT, ASS, SSA, or a ZIP containing subtitle files.\n"
            "📦 Add up to 12 files, then press *▶️ Start Translation*.\n"
            "⚡ Completed files are sent as soon as they are ready, followed by one ZIP.\n\n"
            "Use /help to see commands and options.",
            parse_mode="Markdown",
        )
        if self.state.pending:
            await self._send_queue_keyboard(update.effective_chat.id)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._message(update).reply_text(
            "✨ *BABY 5 — Sinhala Subtitle Bot*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📤 *How to translate*\n"
            "1️⃣ Upload SRT, ASS, SSA, or a ZIP with subtitle files (up to 12 files).\n"
            "2️⃣ Press *▶️ Start Translation* in the queue.\n"
            "3️⃣ Each finished subtitle is sent right away; one ZIP follows when all finish.\n"
            "⏳ An unstarted queue is cleared after 3 minutes.\n\n"
            "⚙️ */settings* — choose batch size (30 / 50 / 100) and translation mode.\n"
            "📊 */status* — check queue, progress, and selected model.\n"
            "🛑 *Stop button* — stop your active translation.\n\n"
            "📝 ASS/SSA files are translated and returned as SRT.\n"
            "🌐 Source subtitles can be in any language; the bot aims to produce natural Sinhala Unicode.\n\n"
            "🔐 *Admin commands*\n"
            "🛡️ */panel* — admin controls.\n"
            "🤖 */change_model* — choose Gemini model (admin only).\n"
            "🧪 */test_gemini* — test Gemini API (admin only).\n"
            "🧹 */reset* — reset bot state (admin only).\n"
            "⛔ */stop* — admin stop for the active job.\n\n"
            "💛 Thanks for using Baby 5!",
            parse_mode="Markdown",
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        st = self.state
        await self._message(update).reply_text(
            "📊 Status\n"
            "Mode: Translate to Sinhala (ASS/SSA auto-convert)\n"
            f"Batch size: {self._get_batch(update.effective_user.id) if update.effective_user else st.batch_size} lines\n"
            f"Mode: {self._get_mode(update.effective_user.id) if update.effective_user else 'normal'}\n"
            f"Queue: {len(st.pending)}/{self.s.max_files}\n"
            f"Processing: {'yes' if st.processing else 'no'}\n"
            f"Selected model: {st.selected_model}\n"
            f"Last API model: {self.gemini.last_used_model}\n"
            f"Gemini keys configured: {sum(bool(k) for k in self.s.gemini_keys)}/10\n"
            f"Translated in current job: {st.translated_successes}\n"
            f"Failed/original kept: {st.failed_subtitles}\n"
            f"Owner: {st.owner_id or 'none'}"
        )

    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user:
            return
        self._user_record(user)
        # Refresh this user's chat-specific command menu so /settings is visible
        # even if an older per-chat command list was cached by Telegram.
        with contextlib.suppress(Exception):
            await self._refresh_user_commands(user.id)
        if self._is_banned(user.id):
            await self._message(update).reply_text("Access denied.")
            return
        if self._is_other_user(user.id):
            await self._busy_message(update)
            return
        batch = self._get_batch(user.id)
        mode = self._get_mode(user.id)
        labels = {"normal": "🐢 Normal mode", "fast": "⚡ Fast mode", "superfast": "🚀 Super Fast mode"}
        await self._message(update).reply_text(
            f"⚙️ *Baby 5 Settings*\n\n📦 Batch size: *{batch} lines*\n🎛️ Translation mode: *{labels[mode]}*\n\nChoose an option below.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Batch size", callback_data="settings:batch"),
                InlineKeyboardButton("🎛️ Modes", callback_data="settings:modes"),
            ]]),
        )

    async def _show_batch_settings(self, query, user_id: int) -> None:
        size = self._get_batch(user_id)
        buttons = [
            [InlineKeyboardButton("Low · 30 lines", callback_data="batch:30")],
            [InlineKeyboardButton("Balanced · 50 lines", callback_data="batch:50")],
            [InlineKeyboardButton("High · 100 lines", callback_data="batch:100")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="settings:home")],
        ]
        await query.edit_message_text(
            f"📦 *Choose Batch Size*\n\nCurrent selection: *{size} lines per batch*\n\nThis setting applies only to your account.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _show_mode_settings(self, query, user_id: int) -> None:
        mode = self._get_mode(user_id)
        labels = {"normal": "🐢 Normal mode", "fast": "⚡ Fast mode", "superfast": "🚀 Super Fast mode"}
        premium = self._is_premium_mode_user(user_id)
        fast_enabled = self._mode_enabled("fast") or premium
        super_enabled = self._mode_enabled("superfast") or premium
        fast_label = "⚡ Fast mode" if fast_enabled else "🔒 Fast mode (OFF)"
        super_label = "🚀 Super Fast mode" if super_enabled else "🔒 Super Fast mode (OFF)"
        buttons = [
            [InlineKeyboardButton("🐢 Normal mode", callback_data="mode:normal")],
            [InlineKeyboardButton(fast_label, callback_data="mode:fast")],
            [InlineKeyboardButton(super_label, callback_data="mode:superfast")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="settings:home")],
        ]
        note = "Choose the translation speed. Your selection applies only to your account."
        if not premium and (not fast_enabled or not super_enabled):
            note += "\n\n🔒 A fast mode switched OFF by the owner cannot be used by regular users."
        await query.edit_message_text(
            f"🎛️ *Translation Modes*\n\nCurrent: *{labels[mode]}*\n\n{note}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ------------------------------------------------------------------
    # Commands — admin
    # ------------------------------------------------------------------

    async def test_gemini(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await self._message(update).reply_text("🔒 `/test_gemini` admin only.")
            return
        message = self._message(update)
        try:
            cfg: dict = {"max_output_tokens": 64}
            if self.state.selected_model.startswith("gemini-3."):
                cfg["thinking_config"] = types.ThinkingConfig(thinking_level="low")
            result = await asyncio.to_thread(
                self.gemini.clients[self.gemini.first_available_key_index()].models.generate_content,
                model=self.state.selected_model,
                contents="Reply with exactly: GEMINI_OK",
                config=types.GenerateContentConfig(**cfg),
            )
            text = (getattr(result, "text", None) or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response")
            await message.reply_text(
                "✅ Gemini API test passed!\n"
                f"🤖 Model: {self.state.selected_model}\n"
                f"🔑 Key slot: {self.gemini.current_key_number()}\n"
                f"💬 Response: {text[:120]}"
            )
        except Exception as exc:
            await message.reply_text(
                "❌ Gemini API test FAILED.\n"
                f"🤖 Model: {self.state.selected_model}\n"
                f"🔑 Key slot: {self.gemini.current_key_number()}\n"
                f"🧾 Error: {self._short_error(exc)}\n\n"
                "Full error: " + repr(exc)[:700]
            )

    async def change_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await self._message(update).reply_text("🔒 `/change_model` admin only.")
            return
        if self.state.processing:
            await self._message(update).reply_text("⏳ A job is running. Model change applies to the next job.")
            return
        if context.args:
            requested = context.args[0].strip()
            if requested in {"gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.5-flash-8b"}:
                await self._message(update).reply_text("❌ Gemini 1.5 models are shut down.")
                return
            if requested not in self.s.models:
                await self._message(update).reply_text(
                    f"❌ Unknown model: `{requested}`\n"
                    f"Available: {', '.join(self.s.models)}\n\n"
                    "Use `/change_model` to see the button list.",
                    parse_mode="Markdown",
                )
                return
            previous_model = self.state.selected_model
            self.state.selected_model = requested
            self.gemini.set_model(requested)
            await self._log_admin_action(update, f"Changed model: {previous_model} -> {requested}")
            label = MODEL_LABELS.get(requested, "")
            await self._message(update).reply_text(
                f"✅ Model changed → `{requested}` {label}",
                parse_mode="Markdown",
            )
            return

        # Button list — one per row, with speed label and current marker
        buttons: list[list[InlineKeyboardButton]] = []
        for model in self.s.models:
            speed = MODEL_LABELS.get(model, "")
            active = "✅ " if model == self.state.selected_model else ""
            label = f"{active}{model}  {speed}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model:{model}")])

        model_info = (
            "\U0001f916 *Select Gemini model*\n\n"
            "\u26a1 Fastest = quickest responses, cheapest\n"
            "\u2696\ufe0f  Balanced = good quality + reasonable speed\n"
            "\U0001f3af Pro = highest quality, slowest\n\n"
            "Current: `" + self.state.selected_model + "`"
        )
        await self._message(update).reply_text(
            model_info,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await self._message(update).reply_text("🔒 Admin only.")
            return
        if self.state.processing:
            await self._signal_stop()
            await self._log_admin_action(update, "Requested reset while a translation job was active")
            await self._message(update).reply_text(
                "🛑 Reset requested — job stopping as soon as possible. Temporary data will be cleared."
            )
            return
        self._clear_release_timer()
        await self._delete_queue_message()
        self._cleanup_job_dir()
        self._hard_clear_state(keep_model=True)
        await self._log_admin_action(update, "Reset queue and temporary state")
        await self._message(update).reply_text("🧹 Reset complete. Queue and temporary files cleared.")

    async def admin_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await self._message(update).reply_text("🔒 Admin only.")
            return

        if not self.state.processing:
            # No active job — just clean up any leftover state
            self._clear_release_timer()
            await self._delete_queue_message()
            self._cleanup_job_dir()
            self._hard_clear_state(keep_model=True)
            await self._log_admin_action(update, "Stopped/reset idle bot state")
            await self._message(update).reply_text("🧹 No active job. State cleared. Bot is ready.")
            return

        # Capture before signalling (state will be wiped when job finishes)
        user_chat_id = self.state.queue_chat_id

        # Signal stop + disable button
        await self._signal_stop()
        await self._message(update).reply_text("🛑 Stop signal sent — waiting for current request to cancel…")

        # Wait for job task to actually finish (it runs _hard_clear_state itself)
        await self._wait_for_job_finish(timeout=10.0)

        # Notify the user whose job was stopped (if different from admin)
        admin_chat_id = update.effective_chat.id if update.effective_chat else None
        if user_chat_id and user_chat_id != admin_chat_id and self.application:
            with contextlib.suppress(Exception):
                await self.application.bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        "🛑 Your translation job was stopped by the bot admin.\n"
                        "Files already translated and sent remain in this chat.\n"
                        "You can start a new job now. 🙂"
                    ),
                )

        await self._log_admin_action(update, "Stopped active translation job")
        await self._message(update).reply_text("✅ Job stopped. Bot is now free for the next user.")

    # ------------------------------------------------------------------
    # Owner/admin management panel
    # ------------------------------------------------------------------

    def _public_commands(self):
        return [
            BotCommand("start", "Welcome and upload subtitles"),
            BotCommand("help", "View the bot guide"),
            BotCommand("status", "View queue and job status"),
            BotCommand("settings", "Choose batch size and translation mode"),
        ]

    def _admin_commands(self):
        return self._public_commands() + [BotCommand("panel", "Admin control panel"), BotCommand("change_model", "Change Gemini model"), BotCommand("reset", "Full reset"), BotCommand("stop", "Stop active job"), BotCommand("test_gemini", "Test Gemini API")]

    def _commands_for_user_id(self, user_id: int):
        if user_id == self.s.admin_user_id:
            return self._admin_commands() + [BotCommand("admin", "Owner: manage admins")]
        if self._db_has("admins", user_id):
            return self._admin_commands()
        return self._public_commands()

    async def _refresh_user_commands(self, user_id: int) -> None:
        # Per-chat command scopes override Telegram's default scope. Always set the
        # correct role-specific list instead of accidentally downgrading admins.
        await self.application.bot.set_my_commands(
            self._commands_for_user_id(user_id),
            scope=BotCommandScopeChat(chat_id=user_id),
        )

    async def _admin_guard(self, update: Update, *, owner_only: bool = False) -> bool:
        ok = self.is_owner(update) if owner_only else self.is_admin(update)
        if not ok and self._message(update):
            await self._message(update).reply_text("Access denied.")
        return ok

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update, owner_only=True):
            return
        await self._show_admin_management(update.effective_message)

    async def panel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin_guard(update):
            return
        await self._show_panel(update.effective_message)

    async def _show_admin_management(self, message) -> None:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Add Admin", callback_data="adm:add")],
            [InlineKeyboardButton("🔴 Remove Admin", callback_data="adm:remove")],
        ])
        await message.reply_text("🛡️ ADMIN MANAGEMENT\nChoose an action.", reply_markup=markup)

    def _panel_markup(self) -> InlineKeyboardMarkup:
        fast_state = "ON" if self._mode_enabled("fast") else "OFF"
        super_state = "ON" if self._mode_enabled("superfast") else "OFF"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Ban / Unban", callback_data="panel:ban"), InlineKeyboardButton("⚙️ Users Settings", callback_data="panel:users")],
            [InlineKeyboardButton("📊 Statistics", callback_data="panel:stats"), InlineKeyboardButton("🔧 Maintenance", callback_data="panel:maintenance")],
            [InlineKeyboardButton(f"⚡ Fast: {fast_state}", callback_data="panel:toggle_fast"), InlineKeyboardButton(f"🚀 Super Fast: {super_state}", callback_data="panel:toggle_superfast")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="panel:broadcast")],
        ])

    async def _show_panel(self, message) -> None:
        await message.reply_text(
            "✨ *BABY 5 — ADMIN PANEL*\n\n🟢 ON / 🔴 OFF controls Fast modes for regular users. Owner and VIP users can still use them.",
            parse_mode="Markdown", reply_markup=self._panel_markup()
        )

    def _back_button(self, callback: str = "panel:home"):
        return [InlineKeyboardButton("⬅️ Back", callback_data=callback)]

    async def _admin_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
        q = update.callback_query
        user = update.effective_user
        if not q or not user:
            return
        if data.startswith("adm:") and not self.is_owner(update):
            await q.answer("Owner only", show_alert=True)
            return
        if data.startswith(("panel:", "users:", "ban:", "vip:", "limit:", "broadcast:", "maint:")) and not self.is_admin(update):
            await q.answer("Access denied", show_alert=True)
            return
        if data in {"panel:toggle_fast", "panel:toggle_superfast"} and not self.is_owner(update):
            await q.answer("Only the bot owner can change global Fast mode settings.", show_alert=True)
            return
        await q.answer()
        if data == "adm:add":
            context.user_data["admin_prompt"] = "add_admin"
            await q.edit_message_text("Send the Telegram user ID or a username already known to this bot.", reply_markup=InlineKeyboardMarkup([self._back_button("adm:home")]))
        elif data == "adm:remove":
            with self._db() as db:
                rows = db.execute("SELECT user_id,username,full_name FROM admins ORDER BY added_at").fetchall()
            buttons = [[InlineKeyboardButton(f"❌ {r['full_name'] or 'Admin'} (@{r['username'] or 'N/A'}) · {r['user_id']}", callback_data=f"adm:removeid:{r['user_id']}")] for r in rows]
            buttons.append(self._back_button("adm:home"))
            await q.edit_message_text("Select an admin to remove:", reply_markup=InlineKeyboardMarkup(buttons))
        elif data.startswith("adm:removeid:"):
            target = int(data.rsplit(":", 1)[1])
            if target == self.s.admin_user_id:
                await q.edit_message_text("The owner cannot be removed.", reply_markup=InlineKeyboardMarkup([self._back_button("adm:home")]))
                return
            with self._db() as db:
                db.execute("DELETE FROM admins WHERE user_id=?", (target,))
            with contextlib.suppress(Exception):
                await self.application.bot.set_my_commands(self._public_commands(), scope=BotCommandScopeChat(chat_id=target))
            await self._log_admin_action(update, f"Removed admin ID {target}")
            await q.edit_message_text(f"Admin {target} removed.", reply_markup=InlineKeyboardMarkup([self._back_button("adm:home")]))
        elif data == "adm:home":
            await q.edit_message_text("🛡️ ADMIN MANAGEMENT", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 Add Admin", callback_data="adm:add")],[InlineKeyboardButton("🔴 Remove Admin", callback_data="adm:remove")]]))
        elif data == "panel:home":
            await q.edit_message_text(
                "✨ *BABY 5 — ADMIN PANEL*\n\n🟢 ON / 🔴 OFF controls Fast modes for regular users. Owner and VIP users can still use them.",
                parse_mode="Markdown", reply_markup=self._panel_markup()
            )
        elif data in {"panel:toggle_fast", "panel:toggle_superfast"}:
            setting_key = "fast_mode_enabled" if data == "panel:toggle_fast" else "superfast_mode_enabled"
            label = "Fast" if data == "panel:toggle_fast" else "Super Fast"
            with self._db() as db:
                row = db.execute("SELECT value FROM settings WHERE key=?", (setting_key,)).fetchone()
                new_value = "0" if row is None or row[0] == "1" else "1"
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (setting_key, new_value))
            await self._log_admin_action(update, f"{label} mode {'enabled' if new_value == '1' else 'disabled'} for regular users")
            await q.edit_message_text(f"{'🟢' if new_value == '1' else '🔴'} {label} mode is now {'ON' if new_value == '1' else 'OFF'} for regular users. Owner and VIP users remain allowed.", reply_markup=InlineKeyboardMarkup([self._back_button()]))
        elif data == "panel:ban":
            await q.edit_message_text("👥 BAN / UNBAN USERS", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔴 Ban User", callback_data="ban:add")],[InlineKeyboardButton("🟢 Unban User", callback_data="ban:remove")],self._back_button()]))
        elif data == "ban:add":
            context.user_data["admin_prompt"] = "ban_user"
            await q.edit_message_text("Send the user ID or a username known to the bot.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:ban")]))
        elif data == "ban:remove":
            with self._db() as db:
                rows = db.execute("SELECT b.user_id,u.username,u.full_name FROM banned b LEFT JOIN users u ON u.user_id=b.user_id ORDER BY b.added_at").fetchall()
            buttons = [[InlineKeyboardButton(f"🟢 {r['full_name'] or 'User'} (@{r['username'] or 'N/A'}) · {r['user_id']}", callback_data=f"ban:unbanid:{r['user_id']}")] for r in rows]
            buttons.append(self._back_button("panel:ban"))
            await q.edit_message_text("Select a banned user to unban:", reply_markup=InlineKeyboardMarkup(buttons))
        elif data.startswith("ban:unbanid:"):
            target = int(data.rsplit(":",1)[1])
            with self._db() as db: db.execute("DELETE FROM banned WHERE user_id=?", (target,))
            await self._log_admin_action(update, f"Unbanned user ID {target}")
            await q.edit_message_text(f"User {target} unbanned.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:ban")]))
        elif data == "panel:users":
            await q.edit_message_text("⚙️ USERS SETTINGS", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("♾️ Remove Limit", callback_data="users:unlimited")],[InlineKeyboardButton("⭐ VIP Members", callback_data="users:vip")],[InlineKeyboardButton("📏 Limit Users", callback_data="users:limits")],[InlineKeyboardButton("🗑️ Remove User", callback_data="users:remove")],self._back_button()]))
        elif data == "users:unlimited":
            context.user_data["admin_prompt"] = "unlimited_user"
            await q.edit_message_text("Send the user ID or known username to remove their daily limit.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")]))
        elif data == "users:vip":
            await q.edit_message_text("⭐ VIP MEMBERS", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add VIP User", callback_data="vip:add")],[InlineKeyboardButton("➖ Remove VIP User", callback_data="vip:remove")],self._back_button("panel:users")]))
        elif data == "vip:add":
            context.user_data["admin_prompt"] = "vip_add"
            await q.edit_message_text("Send the user ID or known username to add as VIP.", reply_markup=InlineKeyboardMarkup([self._back_button("users:vip")]))
        elif data == "vip:remove":
            with self._db() as db: rows = db.execute("SELECT v.user_id,u.username,u.full_name FROM vip v LEFT JOIN users u ON u.user_id=v.user_id ORDER BY v.added_at").fetchall()
            buttons = [[InlineKeyboardButton(f"⭐ {r['full_name'] or 'User'} (@{r['username'] or 'N/A'}) · {r['user_id']}", callback_data=f"vip:removeid:{r['user_id']}")] for r in rows]
            buttons.append(self._back_button("users:vip"))
            await q.edit_message_text("Select a VIP user to remove:", reply_markup=InlineKeyboardMarkup(buttons))
        elif data.startswith("vip:removeid:"):
            target=int(data.rsplit(":",1)[1])
            with self._db() as db: db.execute("DELETE FROM vip WHERE user_id=?",(target,))
            await self._log_admin_action(update, f"Removed VIP user ID {target}")
            await q.edit_message_text(f"VIP removed for user {target}.", reply_markup=InlineKeyboardMarkup([self._back_button("users:vip")]))
        elif data == "users:limits":
            await q.edit_message_text("📏 Choose the daily limit for normal users. VIP and unlimited users are exempt.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("20/day",callback_data="limit:set:20"),InlineKeyboardButton("30/day",callback_data="limit:set:30")],[InlineKeyboardButton("50/day",callback_data="limit:set:50"),InlineKeyboardButton("Custom",callback_data="limit:custom")],self._back_button("panel:users")]))
        elif data.startswith("limit:set:"):
            n=int(data.rsplit(":",1)[1])
            with self._db() as db: db.execute("INSERT INTO settings(key,value) VALUES('daily_limit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(n),))
            await self._log_admin_action(update, f"Set normal daily limit to {n}")
            await q.edit_message_text(f"Normal user daily limit set to {'unlimited' if n == 0 else n} files/day.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")]))
        elif data == "limit:custom":
            context.user_data["admin_prompt"] = "custom_limit"
            await q.edit_message_text("Send a whole number for the normal users' daily limit (0 means unlimited).", reply_markup=InlineKeyboardMarkup([self._back_button("users:limits")]))
        elif data == "users:remove":
            with self._db() as db: rows = db.execute("SELECT user_id,username,full_name FROM users ORDER BY last_seen DESC LIMIT 50").fetchall()
            buttons = [[InlineKeyboardButton(f"{r['full_name'] or 'User'} (@{r['username'] or 'N/A'}) · {r['user_id']}",callback_data=f"users:removeid:{r['user_id']}")] for r in rows]
            buttons.append(self._back_button("panel:users"))
            await q.edit_message_text("Select a user to remove from the bot's user registry:",reply_markup=InlineKeyboardMarkup(buttons))
        elif data.startswith("users:removeid:"):
            target=int(data.rsplit(":",1)[1])
            if target == self.s.admin_user_id or self._db_has("admins", target):
                await q.edit_message_text("Owner/admin accounts cannot be removed here.",reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")]))
                return
            with self._db() as db: db.execute("DELETE FROM users WHERE user_id=?",(target,)); db.execute("DELETE FROM user_settings WHERE user_id=?",(target,))
            await self._log_admin_action(update, f"Removed user registry entry ID {target}")
            await q.edit_message_text(f"User {target} removed from the registry. This does not block them; use Ban User to block access.",reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")]))
        elif data == "panel:stats":
            with self._db() as db:
                total=db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                today=db.execute("SELECT COUNT(*) FROM users WHERE first_seen LIKE ?",(date.today().isoformat()+"%",)).fetchone()[0]
                files=db.execute("SELECT COALESCE(SUM(translations),0) FROM users").fetchone()[0]
                banned=db.execute("SELECT COUNT(*) FROM banned").fetchone()[0]
                vip=db.execute("SELECT COUNT(*) FROM vip").fetchone()[0]
            await q.edit_message_text(f"📊 BOT STATISTICS\\nUsers: {total}\\nNew users today: {today}\\nCompleted files: {files}\\nBanned users: {banned}\\nVIP users: {vip}\\nActive job: {'yes' if self.state.processing else 'no'}",reply_markup=InlineKeyboardMarkup([self._back_button()]))
        elif data == "panel:maintenance":
            with self._db() as db: row=db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
            enabled=bool(row and row[0]=='1')
            await q.edit_message_text(f"🔧 Maintenance mode is {'ON' if enabled else 'OFF'}.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 Turn ON" if not enabled else "🔴 Turn OFF",callback_data="maint:toggle")],self._back_button()]))
        elif data == "maint:toggle":
            with self._db() as db:
                row=db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone(); new='0' if row and row[0]=='1' else '1'
                db.execute("INSERT INTO settings(key,value) VALUES('maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(new,))
            await self._log_admin_action(update, f"Maintenance mode {'ON' if new=='1' else 'OFF'}")
            await q.edit_message_text(f"Maintenance mode {'ON' if new=='1' else 'OFF'}.",reply_markup=InlineKeyboardMarkup([self._back_button()]))
        elif data == "panel:broadcast":
            context.user_data["admin_prompt"]="broadcast"
            await q.edit_message_text("Send the message to broadcast to all registered users. Media and stickers are supported.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel",callback_data="broadcast:cancel")]]))
        elif data == "broadcast:confirm":
            source_chat = context.user_data.pop("broadcast_source_chat", None)
            source_message = context.user_data.pop("broadcast_source_message", None)
            if source_chat is None or source_message is None:
                await q.edit_message_text("Broadcast source expired. Please start /broadcast again.", reply_markup=InlineKeyboardMarkup([self._back_button()]))
                return
            # Disable the confirmation buttons before starting to prevent duplicate sends.
            with contextlib.suppress(Exception):
                await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("Broadcast started. Please wait for the delivery report.")
            await self._execute_broadcast(update, int(source_chat), int(source_message))
            with contextlib.suppress(Exception):
                await q.edit_message_text("Broadcast finished.", reply_markup=InlineKeyboardMarkup([self._back_button()]))
        elif data == "broadcast:cancel":
            context.user_data.pop("admin_prompt",None)
            context.user_data.pop("broadcast_source_chat",None)
            context.user_data.pop("broadcast_source_message",None)
            await q.edit_message_text("Broadcast cancelled.",reply_markup=InlineKeyboardMarkup([self._back_button()]))

    def _resolve_user_id(self, text: str) -> int | None:
        raw=text.strip()
        if raw.startswith("@"):
            raw=raw[1:]
        if raw.isdigit():
            return int(raw)
        with self._db() as db:
            row=db.execute("SELECT user_id FROM users WHERE lower(username)=lower(?)",(raw,)).fetchone()
            if row: return int(row[0])
            row=db.execute("SELECT user_id FROM admins WHERE lower(username)=lower(?)",(raw,)).fetchone()
            return int(row[0]) if row else None

    async def admin_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user=update.effective_user; msg=update.effective_message
        if not user or not msg or not msg.text or not context.user_data.get("admin_prompt"):
            return
        if not self.is_admin(update):
            context.user_data.pop("admin_prompt",None); return
        prompt=context.user_data.get("admin_prompt")
        if prompt == "broadcast":
            await self._broadcast_message(update,context); return
        if prompt == "custom_limit":
            try: n=int(msg.text.strip())
            except ValueError:
                await msg.reply_text("Send a whole number, for example 75."); return
            if n<0 or n>100000:
                await msg.reply_text("Limit must be between 0 and 100000."); return
            with self._db() as db: db.execute("INSERT INTO settings(key,value) VALUES('daily_limit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(n),))
            context.user_data.pop("admin_prompt",None)
            await self._log_admin_action(update,f"Set normal daily limit to {n}")
            await msg.reply_text(f"Normal user daily limit set to {'unlimited' if n==0 else n} files/day.",reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")]))
            return
        target=self._resolve_user_id(msg.text)
        if not target:
            await msg.reply_text("User not found. Use a numeric Telegram ID or a username already known to the bot."); return
        if prompt == "add_admin":
            if target==self.s.admin_user_id:
                await msg.reply_text("The owner already has full access."); return
            with self._db() as db:
                row=db.execute("SELECT username,full_name FROM users WHERE user_id=?",(target,)).fetchone()
                if not row and not msg.text.strip().lstrip("@").isdigit():
                    await msg.reply_text("I cannot resolve an unknown username. Ask the user to start the bot, or send their numeric Telegram ID."); return
                db.execute("INSERT OR REPLACE INTO admins(user_id,username,full_name,added_at) VALUES(?,?,?,?)",(target,row['username'] if row else "",row['full_name'] if row else "",time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime())))
            await self.application.bot.set_my_commands(self._admin_commands(),scope=BotCommandScopeChat(chat_id=target))
            await self._log_admin_action(update,f"Promoted user ID {target} to admin")
            await self._notify_user(target, "🎉 Congratulations! You are now a Baby 5 Bot Admin! 🛡️\n\nYou can now access the admin panel and the admin features assigned to your account. Welcome to the team! ✨")
            context.user_data.pop("admin_prompt",None); await msg.reply_text(f"User {target} promoted to admin.", reply_markup=InlineKeyboardMarkup([self._back_button("adm:home")])); return
        if prompt == "ban_user":
            if target==self.s.admin_user_id or (self._db_has("admins",target) and not self.is_owner(update)):
                await msg.reply_text("You are not allowed to ban the owner or another admin."); return
            with self._db() as db: db.execute("INSERT OR REPLACE INTO banned(user_id,reason,added_at) VALUES(?,?,?)",(target,"Admin action",time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime())))
            await self._log_admin_action(update,f"Banned user ID {target}")
            await self._notify_user(target, "⛔ Your access to this bot has been suspended by an administrator.")
            context.user_data.pop("admin_prompt",None); await msg.reply_text(f"User {target} banned.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:ban")])); return
        if prompt in {"unlimited_user","vip_add"}:
            table="unlimited" if prompt=="unlimited_user" else "vip"
            with self._db() as db: db.execute(f"INSERT OR IGNORE INTO {table}(user_id,added_at) VALUES(?,?)",(target,time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime())))
            await self._log_admin_action(update, f"Granted {'unlimited usage' if table=='unlimited' else 'VIP access'} to user ID {target}")
            context.user_data.pop("admin_prompt",None); await msg.reply_text(f"{'Unlimited access' if table=='unlimited' else 'VIP access'} enabled for user {target}.", reply_markup=InlineKeyboardMarkup([self._back_button("panel:users")])); return

    async def _broadcast_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user=update.effective_user; msg=update.effective_message
        if not user or not msg or not self.is_admin(update): return
        context.user_data.pop("admin_prompt",None)
        context.user_data["broadcast_source_chat"] = msg.chat_id
        context.user_data["broadcast_source_message"] = msg.message_id
        await msg.reply_text(
            f"Preview ready. The message above will be sent to {len(self._registered_user_ids())} registered users. Confirm to continue.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="broadcast:confirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="broadcast:cancel")],
            ])
        )

    async def _execute_broadcast(self, update: Update, source_chat: int, source_message: int) -> None:
        targets=self._registered_user_ids()
        ok=0; failed=0
        for target in targets:
            try:
                await self.application.bot.copy_message(chat_id=target,from_chat_id=source_chat,message_id=source_message)
                ok+=1
                await asyncio.sleep(0.04)
            except Exception:
                failed+=1
        msg = self._message(update)
        if msg:
            await msg.reply_text(f"Broadcast finished. Delivered: {ok}. Failed: {failed}.")
        await self._log_admin_action(update,f"Broadcast delivered to {ok}; failed {failed}")

    async def admin_media_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.user_data.get("admin_prompt") == "broadcast" and self.is_admin(update):
            await self._broadcast_message(update,context)

    # ------------------------------------------------------------------
    # Document upload handler
    # ------------------------------------------------------------------

    async def document_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._message(update)
        user = update.effective_user
        if not message or not user or not message.document:
            return
        if context.user_data.get("admin_prompt") == "broadcast" and self.is_admin(update):
            await self._broadcast_message(update, context)
            return
        self._user_record(user)
        if self._is_banned(user.id):
            await message.reply_text("Access denied.")
            return
        with self._db() as db:
            maintenance = db.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
        if maintenance and maintenance[0] == '1' and not self.is_admin(update):
            await message.reply_text("The bot is temporarily under maintenance. Please try again later.")
            return
        if self._is_other_user(user.id):
            await self._busy_message(update)
            return
        if self.state.processing:
            await message.reply_text("⏳ A job is running. Please wait until it finishes before uploading.")
            return

        # Starting a fresh upload after a completed job clears old state
        if self.state.last_results:
            self._clear_release_timer()
            await self._delete_queue_message()
            self._cleanup_job_dir()
            selected = self.state.selected_model
            batch = self.state.batch_size
            self.state = BotState(selected_model=selected, batch_size=batch)
            self.gemini.set_model(selected)

        filename = message.document.file_name or "uploaded_file"
        suffix = Path(filename).suffix.lower()
        if suffix not in {".srt", ".ass", ".ssa", ".zip"}:
            await message.reply_text("❌ Supported file types: .srt, .ass, .ssa, and .zip.")
            return

        self.state.batch_size = self._get_batch(user.id)
        limit = self._daily_limit(user.id)
        if limit and self._usage_today(user.id) + len(self.state.pending) >= limit:
            await message.reply_text(f"Daily limit reached ({limit} subtitle file(s) per day). Try again tomorrow or contact the bot owner.")
            return
        self._set_user_action(user.id, f"Uploaded {filename}")
        if user.id != self.s.admin_user_id:
            await self._dev_log(f"📥 USER UPLOAD\nName: {user.full_name}\nUsername: @{user.username or 'N/A'}\nUser ID: {user.id}\nFile: {filename}\nTime (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}", source_chat_id=message.chat_id, source_message_id=message.message_id)
        job_dir = self._ensure_work_dir()
        incoming = unique_path(job_dir / "incoming", filename)

        try:
            await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
            if message.document.file_size and message.document.file_size > self.s.max_download_mb * 1024 * 1024:
                raise ValueError(f"Files larger than {self.s.max_download_mb} MB cannot be downloaded.")
            tg_file = await message.document.get_file()
            await tg_file.download_to_drive(custom_path=str(incoming))
            validate_file_size(incoming, self.s.max_download_mb * 1024 * 1024)
        except Exception as exc:
            incoming.unlink(missing_ok=True)
            await message.reply_text(f"❌ Download failed: {self._short_error(exc)}")
            return

        try:
            if suffix == ".zip":
                if len(self.state.pending) >= self.s.max_files:
                    raise ValueError(f"Queue is already full ({self.s.max_files}/{self.s.max_files}).")
                extracted = extract_zip(
                    incoming,
                    job_dir / "incoming" / f"zip_{int(time.time() * 1000)}",
                    self.s.max_zip_members,
                    self.s.max_zip_uncompressed_mb * 1024 * 1024,
                )
                incoming.unlink(missing_ok=True)
                available = self.s.max_files - len(self.state.pending)
                if len(extracted) > available:
                    raise ValueError(
                        f"This ZIP has {len(extracted)} subtitle files but only {available} slot(s) remain. "
                        f"Maximum: {self.s.max_files}."
                    )
                if limit and self._usage_today(user.id) + len(self.state.pending) + len(extracted) > limit:
                    raise ValueError(f"This ZIP would exceed your daily limit of {limit} subtitle file(s).")
                for p in extracted:
                    self.state.pending.append(PendingFile(p, p.name, filename))
            else:
                if len(self.state.pending) >= self.s.max_files:
                    raise ValueError(f"Maximum queue size is {self.s.max_files} files.")
                if limit and self._usage_today(user.id) + len(self.state.pending) + 1 > limit:
                    raise ValueError(f"This upload would exceed your daily limit of {limit} subtitle file(s).")
                self.state.pending.append(PendingFile(incoming, filename, None))

            self._claim_owner(user.id, user.full_name or user.username or "user")
            await self._send_queue_keyboard(update.effective_chat.id)
            self._reset_queue_timeout(update.effective_chat.id)
        except Exception as exc:
            if suffix != ".zip":
                incoming.unlink(missing_ok=True)
            await message.reply_text(f"❌ Could not add this file: {self._short_error(exc)}")

    # ------------------------------------------------------------------
    # Callback (inline buttons)
    # ------------------------------------------------------------------

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        user = update.effective_user
        if not user:
            await query.answer()
            return
        data = query.data or ""

        # Silent no-op for disabled buttons (e.g. "⏳ Stopping…")
        if data == "noop":
            await query.answer()
            return

        if data.startswith(("adm:", "panel:", "users:", "ban:", "vip:", "limit:", "broadcast:", "maint:")):
            await self._admin_callback(update, context, data)
            return

        if data.startswith("settings:"):
            await query.answer()
            if self.state.processing or (self.state.pending and self._is_other_user(user.id)):
                await query.message.reply_text("⏳ Settings cannot be changed while a translation job is active.")
                return
            action = data.split(":", 1)[1]
            if action == "home":
                batch = self._get_batch(user.id)
                mode = self._get_mode(user.id)
                labels = {"normal": "🐢 Normal", "fast": "⚡ Fast", "superfast": "🚀 Super Fast"}
                await query.edit_message_text(
                    f"⚙️ *Baby 5 Settings*\n\n📦 Batch size: *{batch} lines*\n🎛️ Mode: *{labels[mode]}*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📦 Batch size", callback_data="settings:batch"),
                        InlineKeyboardButton("🎛️ Modes", callback_data="settings:modes")
                    ]]),
                )
            elif action == "batch":
                await self._show_batch_settings(query, user.id)
            elif action == "modes":
                await self._show_mode_settings(query, user.id)
            return

        if data.startswith("mode:"):
            if self.state.processing or (self.state.pending and self._is_other_user(user.id)):
                await query.message.reply_text("⏳ Modes cannot be changed while a job is active.")
                return
            mode = data.split(":", 1)[1]
            if mode not in {"normal", "fast", "superfast"}:
                await query.edit_message_text("❌ Invalid mode.")
                return
            if mode in {"fast", "superfast"} and not self._mode_enabled(mode) and not self._is_premium_mode_user(user.id):
                await query.answer("This mode is currently disabled by the bot owner.", show_alert=True)
                await self._show_mode_settings(query, user.id)
                return
            await query.answer()
            self._set_mode(user.id, mode)
            labels = {"normal": "🐢 Normal", "fast": "⚡ Fast", "superfast": "🚀 Super Fast"}
            await query.edit_message_text(
                f"✅ Translation mode set to {labels[mode]}.\nThis setting applies only to your account.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Settings", callback_data="settings:home")
                ]]),
            )
            return

        if data.startswith("batch:"):
            await query.answer()
            if self.state.processing or (self.state.pending and self._is_other_user(user.id)):
                await query.message.reply_text("⏳ Batch settings cannot be changed while a job is active.")
                return
            try:
                size = int(data.split(":", 1)[1])
            except ValueError:
                await query.edit_message_text("❌ Invalid batch selection.")
                return
            if size not in {30, 50, 100}:
                await query.edit_message_text("❌ Invalid batch selection.")
                return
            self._set_batch(user.id, size)
            self._set_user_action(user.id, f"/settings batch {size}")
            if self.is_admin(update):
                await self._log_admin_action(update, f"Changed own batch size to {size}")
            labels = {30: "Low", 50: "Balanced", 100: "High"}
            await query.edit_message_text(
                f"✅ Batch mode set to {labels[size]} ({size} lines per batch).",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Settings", callback_data="settings:home")
                ]]),
            )
            return

        if data == "stop_job":
            if self._is_other_user(user.id):
                await query.answer("🔒 This job belongs to another user.", show_alert=True)
                return
            if not self.state.processing:
                await query.answer("ℹ️ No active job is running.")
                return
            # Signal and disable the button immediately
            await self._signal_stop()
            await query.answer("🛑 Stopping — please wait a moment.")
            return

        if data.startswith("model:"):
            await query.answer()
            if not self.is_admin(update):
                await query.message.reply_text("🔒 Admin only.")
                return
            model = data.split(":", 1)[1]
            if self.state.processing:
                await query.message.reply_text("⏳ A job is running. Model change applies to the next job.")
                return
            if model not in self.s.models:
                await query.message.reply_text("❌ This model is not in the configured list.")
                return
            previous_model = self.state.selected_model
            self.state.selected_model = model
            self.gemini.set_model(model)
            await self._log_admin_action(update, f"Changed model: {previous_model} -> {model}")
            label = MODEL_LABELS.get(model, "")
            await query.message.edit_text(f"✅ Model → {model}  {label}")
            return

        if data == "done":
            await query.answer()
            if self._is_other_user(user.id):
                await query.message.reply_text("⏳ The bot is busy with another user's job.")
                return
            if self.state.processing:
                await query.message.reply_text("⏳ A job is already running.")
                return
            if not self.state.pending:
                await query.message.reply_text("📤 Upload at least one subtitle file first.")
                return
            # Cancel idle-expiry timer
            if self.state.queue_timeout_task and not self.state.queue_timeout_task.done():
                self.state.queue_timeout_task.cancel()
            self.state.queue_timeout_task = None
            # The queue message becomes the live progress card
            self.state.progress_message_id = query.message.message_id
            await query.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔴 Stop", callback_data="stop_job")
                ]])
            )
            self.state.task = asyncio.create_task(
                self._process_job(query.message.chat_id)
            )
            return

        if data == "clear_queue":
            await query.answer()
            if self._is_other_user(user.id):
                await query.message.reply_text("⏳ The bot is busy with another user's job.")
                return
            await self._delete_queue_message()
            self._cleanup_job_dir()
            self._hard_clear_state(keep_model=True)
            await query.message.reply_text("🗑️ Queue cleared. You can upload a new set of files.")
            return

        await query.answer()

    # ------------------------------------------------------------------
    # Queue keyboard
    # ------------------------------------------------------------------

    async def _send_queue_keyboard(self, chat_id: int) -> None:
        if not self.application:
            return
        names = [pf.original_name for pf in self.state.pending]
        visible = []
        for i, name in enumerate(names, start=1):
            clean = name.replace("\n", " ")
            if len(clean) > 64:
                clean = clean[:61] + "…"
            visible.append(f"{i}. {clean}")
        listing = "\n".join(visible) if visible else "(no files yet)"
        full = (
            f"📦 Queue: {len(names)}/{self.s.max_files}\n"
            f"⚙️ Mode: Translate to Sinhala\n🎚️ Batch: {self.state.batch_size} lines\n\n"
            f"{listing}\n\n"
            + (
                "✅ Ready — press Start Translation when all files are uploaded."
                if names else
                "📤 Upload 1–12 subtitle files. This message updates automatically."
            )
        )
        buttons = []
        if names:
            buttons.append([InlineKeyboardButton(
                f"▶️ Start Translation ({len(names)}/{self.s.max_files})",
                callback_data="done",
            )])
            buttons.append([InlineKeyboardButton("❌ Cancel Queue", callback_data="clear_queue")])
        markup = InlineKeyboardMarkup(buttons) if buttons else None

        try:
            if self.state.queue_message_id and self.state.queue_chat_id == chat_id:
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=self.state.queue_message_id,
                    text=full,
                    reply_markup=markup,
                )
                return
        except Exception:
            self.state.queue_message_id = None

        msg = await self.application.bot.send_message(chat_id=chat_id, text=full, reply_markup=markup)
        self.state.queue_message_id = msg.message_id
        self.state.queue_chat_id = chat_id

    async def _delete_queue_message(self) -> None:
        if self.application and self.state.queue_message_id and self.state.queue_chat_id:
            with contextlib.suppress(Exception):
                await self.application.bot.delete_message(
                    chat_id=self.state.queue_chat_id,
                    message_id=self.state.queue_message_id,
                )
        self.state.queue_message_id = None
        self.state.queue_chat_id = None

    async def _busy_message(self, update: Update) -> None:
        await self._message(update).reply_text(
            "⏳ The bot is busy translating another user's subtitles. Please try again when the current job finishes. 💛"
        )

    # ------------------------------------------------------------------
    # Core translation job
    # ------------------------------------------------------------------

    async def _process_job(self, chat_id: int) -> None:
        self.state.processing = True
        self.gemini.reset_rotation()
        if self.state.owner_id:
            self.state.batch_size = self._get_batch(self.state.owner_id)
            self._active_mode = self._get_mode(self.state.owner_id)
            if not self._is_premium_mode_user(self.state.owner_id) and not self._mode_enabled(self._active_mode):
                self._active_mode = "normal"
        else:
            self.state.batch_size = 50
            self._active_mode = "normal"
        self.state.cancel_requested = False
        self.state.cancel_event.clear()
        self.state.error_messages.clear()
        self.state.translated_successes = 0
        self.state.failed_subtitles = 0

        job_dir = self._ensure_work_dir()
        output_dir = job_dir / "outputs"
        total_files = len(self.state.pending)
        progress_done = 0
        file_items: list[tuple[PendingFile, list[SubtitleItem], str, Path]] = []

        try:
            # Make sure we have a progress card
            if not self.state.progress_message_id:
                msg = await self.application.bot.send_message(chat_id=chat_id, text="🧠 Reading subtitle files…")
                self.state.progress_message_id = msg.message_id

            # Pre-read all files so we know total subtitle count before starting
            total_subtitles = 0
            for pf in list(self.state.pending):
                if self.state.cancel_requested:
                    raise asyncio.CancelledError
                try:
                    work_srt = await self._prepare_srt(pf, job_dir)
                    items = read_srt(work_srt)
                    title_hint = detect_context_hint(pf.original_name, pf.source_label)
                    out = unique_path(output_dir, Path(pf.original_name).stem + "__si.srt")
                    file_items.append((pf, items, title_hint, out))
                    total_subtitles += len(items)
                except Exception as exc:
                    self.state.error_messages.append(
                        f"{pf.original_name}: preprocess failed ({self._short_error(exc)}) — skipped"
                    )

            if not file_items:
                await self._update_progress(
                    chat_id, "❌ No valid subtitle files", 0, 1, len(self.state.error_messages),
                    translated_total=0, failed_total=0, progress_total=1,
                )
                await self._delete_queue_message()
                self._cleanup_job_dir()
                self._hard_clear_state(keep_model=True)
                return

            await self._update_progress(
                chat_id, "🧠 Starting…", 0, total_subtitles, len(self.state.error_messages),
                translated_total=0, failed_total=0, progress_total=total_subtitles,
            )
            outputs: list[Path] = []
            glossary: dict[str, str] = {}  # shared glossary across all files in job

            for file_index, (pf, items, title_hint, out) in enumerate(file_items, start=1):
                # Shared success epoch lets a successful parallel batch reset the
                # consecutive-failure round counter of sibling batches.
                rotation_state = {"success_epoch": 0}
                if self.state.cancel_requested:
                    raise asyncio.CancelledError

                translated = [
                    SubtitleItem(x.number, x.start_ms, x.end_ms, x.text, x.timestamp_line)
                    for x in items
                ]
                chunks = build_chunks(items, self.state.batch_size, self.s.max_chars_per_call)

                mode = getattr(self, "_active_mode", "normal")
                if mode == "normal":
                    work_groups = [[(idx, chunk, None)] for idx, chunk in enumerate(chunks, start=1)]
                else:
                    lanes = 3 if mode == "fast" else 5
                    work_groups = []
                    for offset in range(0, len(chunks), lanes):
                        group = []
                        for lane in range(lanes):
                            chunk_pos = offset + lane
                            if chunk_pos >= len(chunks):
                                continue
                            if mode == "fast":
                                # Fast mode uses three primary lanes: 1→4→7,
                                # 2→5→8, 3→6→9. Key 10 remains unused/free.
                                fast_lanes = ([0, 3, 6], [1, 4, 7], [2, 5, 8])
                                key_indices = list(fast_lanes[lane])
                            else:
                                # Super Fast remains five lanes: 1→6, 2→7,
                                # 3→8, 4→9, 5→10.
                                key_indices = list(range(lane, len(self.gemini.clients), 5))
                            group.append((chunk_pos + 1, chunks[chunk_pos], key_indices))
                        work_groups.append(group)

                for work_group in work_groups:
                    if self.state.cancel_requested:
                        raise asyncio.CancelledError

                    async def translate_one(chunk_index, chunk, key_indices):
                        first_pos = items.index(chunk[0])
                        last_pos = first_pos + len(chunk)
                        previous = items[max(0, first_pos - self.s.context_entries):first_pos]
                        following = items[last_pos:last_pos + self.s.context_entries]
                        return chunk_index, chunk, await self._translate_chunk_with_split(
                            chunk=chunk, previous=previous, following=following,
                            title_hint=title_hint, glossary=glossary,
                            source_name=pf.original_name, key_indices=key_indices,
                            rotation_state=rotation_state,
                        )

                    pending_work = list(work_group)
                    while pending_work:
                        if self.state.cancel_requested:
                            raise asyncio.CancelledError
                        task_map = {
                            asyncio.create_task(translate_one(chunk_index, chunk, key_indices)): (chunk_index, chunk, key_indices)
                            for chunk_index, chunk, key_indices in pending_work
                        }
                        failures = []
                        successes = []

                        # Consume each finished request immediately. Previously gather()
                        # waited for the slowest parallel lane before updating progress,
                        # making the progress bar lag behind successful API log entries.
                        while task_map:
                            if self.state.cancel_requested:
                                for task in task_map:
                                    task.cancel()
                                await asyncio.gather(*task_map, return_exceptions=True)
                                raise asyncio.CancelledError
                            done_tasks, _ = await asyncio.wait(
                                task_map, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in done_tasks:
                                work_item = task_map.pop(task)
                                try:
                                    result = task.result()
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    failures.append((work_item, exc))
                                    continue
                                successes.append(result)
                                chunk_index, chunk, (t_map, failed_ids) = result
                                for sub_id, translated_text in t_map.items():
                                    for target in translated:
                                        if target.number == sub_id:
                                            target.text = translated_text
                                            break
                                self.state.translated_successes += len(t_map)
                                self.state.failed_subtitles += len(failed_ids)
                                progress_done += len(t_map) + len(failed_ids)
                                await self._update_progress(
                                    chat_id,
                                    f"🎬 Translating {file_index}/{total_files} · {pf.original_name}",
                                    progress_done, total_subtitles, len(self.state.error_messages),
                                    translated_total=self.state.translated_successes,
                                    failed_total=self.state.failed_subtitles,
                                    progress_total=total_subtitles,
                                )

                        if self.state.cancel_requested:
                            raise asyncio.CancelledError
                        if not failures:
                            break

                        # If any sibling batch succeeded, that success resets the
                        # shared round budget. Retry only failed chunks instead of
                        # cancelling other lanes or incorrectly failing the job.
                        if successes:
                            pending_work = [item for item, _exc in failures]
                            print(
                                f"[{__import__('datetime').datetime.now().strftime('%H:%M:%S')}] "
                                f"[ROTATION-RESET] {len(successes)} parallel batch(es) succeeded; "
                                f"retrying {len(pending_work)} failed batch(es) from round 1",
                                flush=True,
                            )
                            continue

                        # No batch succeeded in this group and all failed batches
                        # exhausted their 10 consecutive rounds: stop with the
                        # existing user-facing failure message.
                        raise failures[0][1]

                    if mode == "normal" and work_group and work_group[0][0] < len(chunks):
                        await asyncio.sleep(self.gemini.effective_inter_call_delay)

                write_srt(translated, out)
                self._add_credit_cues(out, translated)
                outputs.append(out)
                await self._send_single_output(chat_id, out, len(outputs), total_files)
                # Admin-system bookkeeping only; translation flow above remains the original implementation.
                owner_id = self.state.owner_id
                if owner_id:
                    self._increment_usage(owner_id, 1)
                    if owner_id != self.s.admin_user_id:
                        with self._db() as db:
                            row = db.execute("SELECT username, full_name FROM users WHERE user_id=?", (owner_id,)).fetchone()
                        who = f"{row['full_name'] if row else 'Unknown'} (@{row['username'] if row and row['username'] else 'N/A'}, ID {owner_id})"
                        await self._dev_log(f"📤 TRANSLATION OUTPUT\nUser: {who}\nOutput: {out.name}\nModel: {self.state.selected_model}\nStatus: completed\nTime (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}", file_path=out)

            # ── Job complete ──
            self.state.last_results = LastResults(outputs, job_dir, chat_id, time.time())

            await self._update_progress(
                chat_id, "✅ Subtitle translation complete!",
                progress_done, total_subtitles, len(self.state.error_messages),
                translated_total=self.state.translated_successes,
                failed_total=self.state.failed_subtitles,
                progress_total=total_subtitles,
            )

            summary = (
                "🎉 Subtitle translation complete!\n"
                f"📁 {len(outputs)}/{total_files} output file(s) sent.\n"
            )
            if total_subtitles:
                summary += f"🇱🇰 Sinhala translated: {self.state.translated_successes}/{total_subtitles} subtitle line(s)."
            if any(pf.path.suffix.lower() in {".ass", ".ssa"} for pf, _, _, _ in file_items):
                summary += "\n📝 ASS/SSA subtitles were translated and delivered as SRT files."
            if self.state.failed_subtitles:
                summary += f"\n⚠️ Original kept after translation failure: {self.state.failed_subtitles} line(s)."
            err_text = self._format_errors()
            if err_text:
                summary += "\n\n" + err_text
            await self.application.bot.send_message(chat_id=chat_id, text=summary)
            await self._send_zip(chat_id)
            await self._delete_queue_message()
            self._cleanup_job_dir()
            self._hard_clear_state(keep_model=True)

        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="🛑 Job stopped. Queue and temporary files have been cleared.",
                )
            await self._delete_queue_message()
            self._cleanup_job_dir()
            self._hard_clear_state(keep_model=True)

        except Exception as exc:
            self.state.error_messages.append(f"JOB: {self._short_error(exc)}")
            job_user_id = self.state.owner_id
            if job_user_id and job_user_id != self.s.admin_user_id:
                with self._db() as db:
                    job_user = db.execute("SELECT username,full_name FROM users WHERE user_id=?", (job_user_id,)).fetchone()
                await self._dev_log(f"⚠️ TRANSLATION FAILED\nUser: {job_user['full_name'] if job_user else 'Unknown'}\nUsername: @{job_user['username'] if job_user and job_user['username'] else 'N/A'}\nUser ID: {job_user_id}\nModel: {self.state.selected_model}\nError: {self._short_error(exc)}\nTime (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
            if isinstance(exc, GeminiQuotaExhaustedError):
                note = (
                    "⛔ Gemini API quota/rate limit reached on all configured keys.\n"
                    "Check the keys' quotas in Google AI Studio, then start the job again.\n\n"
                    "Completed files already sent remain in your chat."
                )
            elif isinstance(exc, GeminiServiceUnavailableError):
                note = (
                    "☁️ Gemini is temporarily unavailable (503).\n"
                    "The bot stopped after all 10 key-rotation rounds failed. Try again later.\n\n"
                    "Completed files already sent remain in your chat."
                )
            elif isinstance(exc, GeminiBatchFailedError):
                note = (
                    "❌ Translation failed: this subtitle batch did not succeed after all 10 key-rotation rounds.\n"
                    "The bot stopped this job instead of silently changing the batch or model.\n\n"
                    "Completed files already sent remain in your chat."
                )
            else:
                note = (
                    "⚠️ The job stopped unexpectedly. "
                    "Completed files already sent remain in your chat.\n\n"
                    + self._format_errors()
                )
            with contextlib.suppress(Exception):
                await self.application.bot.send_message(chat_id=chat_id, text=note)
            await self._delete_queue_message()
            self._cleanup_job_dir()
            self._hard_clear_state(keep_model=True)

        finally:
            self.gemini.reset_rotation()
            self.state.processing = False
            self.state.progress_message_id = None
            self.state.task = None

    # ------------------------------------------------------------------
    # Translation helpers
    # ------------------------------------------------------------------

    async def _prepare_srt(self, pf: PendingFile, job_dir: Path) -> Path:
        suffix = pf.path.suffix.lower()
        if suffix == ".srt":
            return pf.path
        if suffix in {".ass", ".ssa"}:
            converted = unique_path(job_dir / "converted", pf.path.stem + ".srt")
            return ass_to_srt(pf.path, converted)
        raise ValueError(f"Unsupported subtitle type: {pf.original_name}")

    async def _translate_chunk_with_split(
        self,
        chunk: list[SubtitleItem],
        previous: list[SubtitleItem],
        following: list[SubtitleItem],
        title_hint: str,
        glossary: dict[str, str],
        source_name: str,
        _relaxed: bool = False,
        key_indices: list[int] | None = None,
        rotation_state: dict[str, int] | None = None,
    ) -> tuple[dict[int, str], set[int]]:
        if self.state.cancel_requested:
            raise asyncio.CancelledError

        # After a failed large batch, temporarily process 25-line chunks.
        # Three consecutive successful small chunks restore normal 50-line
        # processing for the remaining queue.
        adaptive_size = getattr(self, "_adaptive_batch_size", 50)
        if len(chunk) > 25 and adaptive_size == 25:
            mid = 25
            left = await self._translate_chunk_with_split(
                chunk[:mid], previous, chunk[mid:mid + self.s.context_entries],
                title_hint, glossary, source_name, key_indices=key_indices,
                rotation_state=rotation_state,
            )
            right = await self._translate_chunk_with_split(
                chunk[mid:], chunk[:self.s.context_entries], following,
                title_hint, glossary, source_name, key_indices=key_indices,
                rotation_state=rotation_state,
            )
            return ({**left[0], **right[0]}, left[1] | right[1])

        try:
            result = await self.gemini.translate_batch(
                batch=chunk,
                previous_context=previous,
                next_context=following,
                title_hint=title_hint,
                glossary=glossary,
                retries_per_attempt=self.s.retry_per_attempt,
                cancel_event=self.state.cancel_event,
                relaxed_script=_relaxed,
                key_indices=key_indices, rotation_state=rotation_state,
            )
            glossary.update(result.glossary_updates)
            if len(chunk) == 25 and getattr(self, "_adaptive_batch_size", 50) == 25:
                self._adaptive_small_successes = getattr(self, "_adaptive_small_successes", 0) + 1
                if self._adaptive_small_successes >= 3:
                    self._adaptive_batch_size = 50
                    self._adaptive_small_successes = 0
                    print(
                        f"[{__import__('datetime').datetime.now().strftime('%H:%M:%S')}] "
                        "[BATCH-ADAPT] 3 successful 25-line batches; restoring 50-line batches",
                        flush=True,
                    )
            return result.translations, set()
        except asyncio.CancelledError:
            raise
        except (GeminiQuotaExhaustedError, GeminiServiceUnavailableError, GeminiBatchFailedError):
            raise
        except Exception as exc:
            err_str = self._short_error(exc)
            stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")

            if len(chunk) > 25:
                self._adaptive_batch_size = 25
                self._adaptive_small_successes = 0

            if len(chunk) > 1:
                # ── Fast isolation: if error names a specific ID, split so that
                #    ID is alone immediately rather than binary-splitting the whole
                #    batch down through many levels.
                bad_id: int | None = None
                import re as _re
                m = _re.search(r"\bID\s+(\d+)\b", err_str)
                if m:
                    candidate = int(m.group(1))
                    positions = [i for i, s in enumerate(chunk) if s.number == candidate]
                    if positions:
                        bad_id = candidate
                        bad_pos = positions[0]

                if bad_id is not None:
                    # Split around the known-bad subtitle
                    good_before = chunk[:bad_pos]
                    bad_sub     = chunk[bad_pos:bad_pos + 1]
                    good_after  = chunk[bad_pos + 1:]
                    print(
                        f"[{stamp}] [FAST-ISOLATE] file={source_name} "
                        f"bad_id={bad_id} chunk={len(chunk)} "
                        f"-> {len(good_before)}+1+{len(good_after)}",
                        flush=True,
                    )
                    all_t: dict[int, str] = {}
                    all_f: set[int] = set()
                    for part in [good_before, bad_sub, good_after]:
                        if not part:
                            continue
                        if self.state.cancel_requested:
                            raise asyncio.CancelledError
                        t, f = await self._translate_chunk_with_split(
                            part, previous, following, title_hint, glossary, source_name,
                            _relaxed=(len(part) == 1), key_indices=key_indices,
                            rotation_state=rotation_state,
                        )
                        all_t.update(t)
                        all_f.update(f)
                    return all_t, all_f

                # Normal binary split
                mid = len(chunk) // 2
                print(
                    f"[{stamp}] [BATCH-SPLIT] file={source_name} subs={len(chunk)} "
                    f"reason={err_str[:160]} -> {mid}+{len(chunk) - mid}",
                    flush=True,
                )
                if self.state.cancel_requested:
                    raise asyncio.CancelledError
                left = await self._translate_chunk_with_split(
                    chunk[:mid], previous, following, title_hint, glossary, source_name,
                    key_indices=key_indices, rotation_state=rotation_state,
                )
                if self.state.cancel_requested:
                    raise asyncio.CancelledError
                right = await self._translate_chunk_with_split(
                    chunk[mid:], chunk[:min(2, len(chunk))], following, title_hint, glossary, source_name,
                    key_indices=key_indices, rotation_state=rotation_state,
                )
                return ({**left[0], **right[0]}, left[1] | right[1])

            # ── Single subtitle last resort ───────────────────────────────
            if not _relaxed:
                # One more attempt with relaxed script checking before giving up
                print(
                    f"[{stamp}] [RELAX-RETRY] file={source_name} "
                    f"sub=#{chunk[0].number} — retrying without script check",
                    flush=True,
                )
                try:
                    return await self._translate_chunk_with_split(
                        chunk, previous, following, title_hint, glossary, source_name,
                        _relaxed=True, key_indices=key_indices,
                        rotation_state=rotation_state,
                    )
                except (asyncio.CancelledError, GeminiQuotaExhaustedError, GeminiServiceUnavailableError):
                    raise
                except Exception:
                    pass  # fall through to keep-original

            # Truly unreachable — keep original text
            item = chunk[0]
            self.state.error_messages.append(
                f"{source_name} #{item.number}: {err_str[:120]} — original kept"
            )
            return {}, {item.number}

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _add_credit_cues(self, path: Path, items: list[SubtitleItem]) -> None:
        if not items:
            return
        try:
            raw = path.read_text(encoding="utf-8")
            credit = '<font color="#1687ff">Translate by @Baby_5_advancetr_bot</font>'
            first = f"0\n00:00:00,000 --> 00:00:10,000\n{credit}"
            last_end = max((x.end_ms for x in items), default=3000)
            end_start = max(0, last_end - 10000)
            end = (
                f"{max((x.number for x in items), default=0) + 1}\n"
                f"{format_srt_time(end_start)} --> {format_srt_time(last_end)}\n{credit}"
            )
            path.write_text(first + "\n\n" + raw.rstrip() + "\n\n" + end + "\n", encoding="utf-8")
        except Exception as exc:
            self.state.error_messages.append(f"{path.name}: credit overlay skipped ({self._short_error(exc)})")

    async def _send_single_output(self, chat_id: int, path: Path, index: int, total: int) -> None:
        try:
            await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            if path.stat().st_size > 50 * 1024 * 1024:
                self.state.error_messages.append(f"{path.name}: output exceeds Telegram 50 MB limit")
                return
            with path.open("rb") as fh:
                await self.application.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(fh, filename=path.name),
                    caption=f"✅ Output {index}/{total} · {path.name}",
                )
            await asyncio.sleep(self.s.send_delay)
        except Exception as exc:
            self.state.error_messages.append(f"{path.name}: send failed ({self._short_error(exc)})")

    async def _send_zip(self, chat_id: int) -> None:
        if not self.state.last_results:
            return
        base = self.state.last_results.base_dir
        zip_path = unique_path(base, "translated_sinhala_subtitles.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in self.state.last_results.output_paths:
                    if p.exists():
                        zf.write(p, arcname=p.name)
            if zip_path.stat().st_size > 50 * 1024 * 1024:
                raise ValueError("ZIP exceeds Telegram's 50 MB send limit.")
            with zip_path.open("rb") as fh:
                await self.application.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(fh, filename="translated_sinhala_subtitles.zip"),
                    caption="📦 Your complete subtitle ZIP is ready. ✨",
                )
        except Exception as exc:
            self.state.error_messages.append(f"ZIP: {self._short_error(exc)}")
            with contextlib.suppress(Exception):
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Could not send the ZIP: {self._short_error(exc)}",
                )

    # ------------------------------------------------------------------
    # Progress bar
    # ------------------------------------------------------------------

    async def _update_progress(
        self,
        chat_id: int,
        label: str,
        done: int,
        total: int,
        errors: int,
        *,
        translated_total: int,
        failed_total: int,
        progress_total: int | None = None,
    ) -> None:
        denom = max(progress_total if progress_total is not None else total, 1)
        pct = min(100, int(done * 100 / denom))
        filled = min(10, pct // 10)
        bar = "▰" * filled + "▱" * (10 - filled)

        lines = [
            "✨ 𝐁𝐀𝐁𝐘 𝟓  ·  SINHALA LIVE",
            "━━━━━━━━━━━━━━━━━━",
            label,
            f"{bar}  {pct}%",
            f"📍 Processed  {min(done, denom):,}/{denom:,} lines",
        ]
        if total:
            lines.append(f"🇱🇰 Translated  {translated_total:,}/{total:,}")
        else:
            lines.append("📝 ASS/SSA conversion only — no translation requested")
        mode_labels = {"normal": "Normal", "fast": "Fast", "superfast": "Super Fast"}
        lines.append(
            f"⚙️ {mode_labels.get(getattr(self, '_active_mode', 'normal'), 'Normal')} mode"
            f" · Batch {self.state.batch_size} · {self.state.selected_model}"
        )
        lines.append(f"📦 Queue  {len(self.state.pending)}/{self.s.max_files} files")
        if failed_total:
            lines.append(f"⚠️ Original kept  {failed_total:,} line(s)")
        if errors:
            recent = self.state.error_messages[-1:]
            lines.append("⚠️ Latest issue")
            lines.extend(f"• {x}" for x in recent)
            if errors > 1:
                lines.append(f"• +{errors - 1} more issue(s)")

        text = "\n".join(lines)
        # Skip edit if nothing changed — avoids Telegram "message not modified" errors
        # and unnecessary API calls that slow the job down.
        if text == self.state._last_progress_text:
            return
        self.state._last_progress_text = text
        if self.state.progress_message_id and self.application:
            with contextlib.suppress(Exception):
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=self.state.progress_message_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔴 Stop", callback_data="stop_job")
                    ]]),
                )


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    public_commands = [
        BotCommand("start", "Welcome and upload subtitles"),
        BotCommand("help", "View the bot guide"),
        BotCommand("status", "View queue and job status"),
        BotCommand("settings", "Choose batch size and translation mode"),
    ]
    settings = application.bot_data["settings"]
    bot = application.bot_data["subtitle_bot"]
    # Default menu: normal users see only the four public commands.
    await application.bot.delete_my_commands(scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
    # Explicit all-private-chats scope helps replace stale menus from older versions.
    await application.bot.set_my_commands(public_commands, scope=BotCommandScopeAllPrivateChats())

    # Refresh per-chat scopes for all known users so stale command menus from
    # earlier versions cannot hide admin commands or expose owner-only commands.
    with bot._db() as db:
        known_users = {int(r[0]) for r in db.execute("SELECT user_id FROM users")}
        known_users.update(int(r[0]) for r in db.execute("SELECT user_id FROM admins"))
    known_users.add(settings.admin_user_id)
    for user_id in known_users:
        with contextlib.suppress(Exception):
            await bot._refresh_user_commands(user_id)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if isinstance(exc, NetworkError):
        print(f"[Telegram network] {exc.__class__.__name__}: {str(exc)[:160]}")
    else:
        print(f"[Bot error] {exc.__class__.__name__}: {str(exc)[:220]}")


def build_application(settings: Settings) -> Application:
    app = (
        Application.builder()
        .token(settings.bot_token)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(30.0)
        .post_init(_post_init)
        .build()
    )
    bot = SubtitleBot(settings)
    bot.application = app

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("admin", bot.admin_command))
    app.add_handler(CommandHandler("panel", bot.panel_command))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("status", bot.status))
    app.add_handler(CommandHandler("settings", bot.settings_command))
    app.add_handler(CommandHandler("test_gemini", bot.test_gemini))
    app.add_handler(CommandHandler("change_model", bot.change_model))
    app.add_handler(CommandHandler("reset", bot.reset))
    app.add_handler(CommandHandler("stop", bot.admin_stop))
    app.add_handler(CallbackQueryHandler(bot.callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.admin_text_handler), group=1)
    app.add_handler(MessageHandler(filters.ALL, bot.admin_media_handler), group=1)
    app.add_error_handler(_error_handler)

    app.bot_data["subtitle_bot"] = bot
    app.bot_data["settings"] = settings
    return app


def main():
    settings = Settings.from_env()
    missing_keys = [str(i + 1) for i, key in enumerate(settings.gemini_keys) if not key]
    if missing_keys:
        print(f"[CONFIG] Missing GEMINI_API_KEY_{', GEMINI_API_KEY_'.join(missing_keys)}; key slots are preserved and skipped.", flush=True)
    print(f"[CONFIG] Gemini API keys configured: {sum(bool(k) for k in settings.gemini_keys)}/10", flush=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = build_application(settings)
    print("Anime/Sinhala subtitle bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
