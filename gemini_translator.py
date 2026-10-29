from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from prompt_rules import TRANSLATION_SYSTEM_PROMPT
from subtitle_utils import SubtitleItem, normalize_text


def _live_log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] [GEMINI] {message}", flush=True)


class TranslationPayloadError(ValueError):
    """Raised when Gemini returns JSON that cannot be mapped to the requested subtitles."""


class GeminiQuotaExhaustedError(RuntimeError):
    """Raised when every configured API key is quota/rate limited for this request."""


class GeminiServiceUnavailableError(RuntimeError):
    """Raised when Gemini remains unavailable after all bounded retries."""


class GeminiBatchFailedError(RuntimeError):
    """Raised when a batch exhausts all 10 full key-rotation rounds."""


@dataclass
class GeminiCallResult:
    translations: dict[int, str]
    glossary_updates: dict[str, str]
    used_model: str
    used_key_index: int


# ---------------------------------------------------------------------------
# Pressure tracker — records consecutive 503s so we can slow down inter-call
# delays when the API is under load.  Resets automatically after a quiet period.
# ---------------------------------------------------------------------------
@dataclass
class _PressureState:
    consecutive_503: int = 0
    _last_503_time: float = field(default_factory=time.monotonic)
    _COOL_AFTER: float = 90.0   # seconds of silence before auto-reset

    def hit(self) -> None:
        self.consecutive_503 += 1
        self._last_503_time = time.monotonic()

    def success(self) -> None:
        if self.consecutive_503 > 0:
            self.consecutive_503 = max(0, self.consecutive_503 - 1)

    def reset(self) -> None:
        """Clear pressure between independent jobs/stops."""
        self.consecutive_503 = 0
        self._last_503_time = time.monotonic()

    def _maybe_cool(self) -> None:
        if self.consecutive_503 > 0 and (time.monotonic() - self._last_503_time) > self._COOL_AFTER:
            self.consecutive_503 = 0

    def inter_call_delay(self, base: float) -> float:
        """Scale the between-batch delay gently under pressure (max 3× base, cap 10 s)."""
        self._maybe_cool()
        if self.consecutive_503 == 0:
            return base
        multipliers = [1.5, 2.0, 3.0, 3.0]
        idx = min(self.consecutive_503 - 1, len(multipliers) - 1)
        return min(base * multipliers[idx], 10.0)

    def persistent_wait(self, base: float = 30.0) -> float:
        """Cooldown used only when ALL keys have failed; backs off up to 120 s."""
        self._maybe_cool()
        factor = min(1.5 ** max(0, self.consecutive_503 - 1), 4.0)
        return min(base * factor, 120.0)


_pressure = _PressureState()


class GeminiPool:
    def __init__(
        self,
        keys: tuple[str, ...],
        model: str,
        models: tuple[str, ...],
        auto_model_fallback: bool = True,
    ):
        self.keys = keys
        self.model = model
        self.models = models
        self.auto_model_fallback = auto_model_fallback
        self._key_index = 0
        self._lock = asyncio.Lock()
        # Preserve the original key slots (1..10); blank environment variables
        # are skipped without shifting the remaining key-to-lane assignments.
        self.clients = [genai.Client(api_key=k) if k else None for k in keys]
        self.last_used_model = model
        self.base_inter_call_delay: float = 1.2
        self._payload_sweep_done: bool = False

    def set_model(self, model: str) -> None:
        self.model = model
        self.last_used_model = model

    def current_key_number(self) -> int:
        return self._key_index + 1

    def reset_rotation(self) -> None:
        """Start each new/aborted job from API key 1 and clear old pressure."""
        self._key_index = 0
        _pressure.reset()

    def first_available_key_index(self) -> int:
        return next((i for i, client in enumerate(self.clients) if client is not None), 0)

    @property
    def effective_inter_call_delay(self) -> float:
        return _pressure.inter_call_delay(self.base_inter_call_delay)

    async def _rotate_key(self) -> None:
        async with self._lock:
            self._key_index = (self._key_index + 1) % len(self.clients)

    # ------------------------------------------------------------------
    # Error classifiers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_quota(exc: Exception) -> bool:
        t = str(exc).lower()
        return any(m in t for m in ("429", "resource_exhausted", "quota", "rate limit"))

    @staticmethod
    def _is_503(exc: Exception) -> bool:
        t = str(exc).lower()
        return any(m in t for m in ("503", "unavailable", "high demand", "temporarily unavailable"))

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        # TranslationPayloadError is retryable — the corrected prompt may fix it
        if isinstance(exc, TranslationPayloadError):
            return True
        t = str(exc).lower()
        return any(m in t for m in (
            "429", "resource_exhausted", "quota", "rate limit",
            "503", "500", "unavailable", "deadline", "timeout", "internal",
        ))

    @staticmethod
    def _is_model_error(exc: Exception) -> bool:
        t = str(exc).lower()
        # 404 ClientError = model doesn't exist → skip all keys, go to next model
        if "404" in t and ("not_found" in t or "not found" in t):
            return True
        return any(m in t for m in (
            "model not found", "unsupported model", "invalid model",
            "unknown model", "does not support", "no longer available",
            "permission denied", "access denied", "disabled",
        ))

    # ------------------------------------------------------------------
    # Script validation
    # ------------------------------------------------------------------
    @staticmethod
    def _bad_script(text: str) -> bool:
        """Return True only for genuine forbidden scripts or model artefact tokens.

        Be PERMISSIVE: accept pure-English lines, mixed Sinhala+English, names,
        sound words (Hmm, Yeah, OK).  Only hard-reject characters from scripts that
        can never appear in a valid Sinhala subtitle (Devanagari, Tamil, Telugu,
        Malayalam, Hangul, CJK, Arabic, Hebrew …) and model control tokens.
        """
        # Model artefact tokens — always bad
        if re.search(
            r"<\s*(?:bos|eos|pad|unk|s|/s)\s*>|<\|[^>]+\|>|\[UNK\]",
            text,
            re.IGNORECASE,
        ):
            return True

        # Scan every letter/mark character for a forbidden script
        for ch in text:
            cat = unicodedata.category(ch)
            if cat[0] not in {"L", "M"}:
                continue  # punctuation, digits, spaces, emoji → always fine
            cp = ord(ch)
            # ✅ Sinhala block (U+0D80–U+0DFF)
            if 0x0D80 <= cp <= 0x0DFF:
                continue
            # ✅ Latin: Basic + Extended-A/B + Additional
            if (0x0041 <= cp <= 0x007A      # A-Z a-z
                    or 0x00C0 <= cp <= 0x024F
                    or 0x1E00 <= cp <= 0x1EFF):
                continue
            # ❌ Everything else is a foreign script character
            return True

        # All characters passed — accept the line regardless of whether it
        # contains Sinhala or is pure Latin (names, interjections, etc.)
        return False

    # ------------------------------------------------------------------
    # Cancellable helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _cancellable_sleep(seconds: float, cancel_event: asyncio.Event | None) -> None:
        """Sleep for `seconds` but wake immediately if cancel_event is set."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(min(0.3, deadline - time.monotonic()))

    @staticmethod
    async def _cancellable_api_call(coro, cancel_event: asyncio.Event | None):
        """Run a thread-offloaded API call; cancel it instantly if cancel_event fires."""
        task = asyncio.ensure_future(coro)
        try:
            while not task.done():
                if cancel_event and cancel_event.is_set():
                    task.cancel()
                    raise asyncio.CancelledError
                await asyncio.sleep(0.3)
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    # ------------------------------------------------------------------
    # Main translation entry point
    # ------------------------------------------------------------------
    async def translate_batch(
        self,
        batch: list[SubtitleItem],
        previous_context: list[SubtitleItem],
        next_context: list[SubtitleItem],
        title_hint: str,
        glossary: dict[str, str],
        retries_per_attempt: int = 2,
        cancel_event: asyncio.Event | None = None,
        relaxed_script: bool = False,
        key_indices: list[int] | None = None,
        rotation_state: dict[str, int] | None = None,
    ) -> GeminiCallResult:

        def _item(s: SubtitleItem) -> dict:
            return {"id": s.number, "start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text}

        payload = {
            "title_hint": title_hint or "",
            "source_language_instruction": "Detect automatically; source is not necessarily Japanese.",
            "glossary": dict(list(glossary.items())[-120:]),
            "previous_subtitles": [_item(x) for x in previous_context],
            "current_subtitles": [_item(x) for x in batch],
            "following_subtitles": [_item(x) for x in next_context],
        }
        base_prompt = (
            "You are translating subtitles into NATURAL SPOKEN SRI LANKAN SINHALA.\n\n"
            "━━━ TRANSLATION PRIORITY (read before translating) ━━━\n"
            "1. NATURAL spoken Sinhala — write how real Sri Lankans actually speak, NOT textbook Sinhala.\n"
            "   Bad:  'ඔබ කොතැනකටද යන්නේ?' (textbook, stiff)\n"
            "   Good: 'ඔයා කොහෙද යන්නේ?' (natural, spoken)\n\n"
            "2. Every WORD must be in Sinhala script — NOT English words written for Sinhala meaning.\n"
            "   Bad:  'මමත් එකට යන්න want කරනවා'\n"
            "   Good: 'මමත් එකට යන්න ඕනේ'\n\n"
            "3. FEEL matters — keep emotion, humor, anger, sarcasm, urgency. Don't flatten tone.\n"
            "   Angry line → strong direct Sinhala.  Funny line → keep the joke in Sinhala.\n\n"
            "4. SHORT and READABLE — subtitles must be fast to read. Cut filler, keep meaning.\n\n"
            "5. Names, brands, techniques stay in Latin (e.g. Naruto, FBI, YouTube). Everything else → Sinhala.\n\n"
            "━━━ INPUT DATA ━━━\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n━━━ OUTPUT ━━━\n"
            "Return ONLY this JSON (no markdown, no extra text):\n"
            '{"results":[{"id":123,"translation":"සිංහල උපසිරැස් පෙළ"}],"glossary_updates":{}}\n\n'
            "Rules:\n"
            "- One result object per current_subtitles item, same numeric id\n"
            "- translation field: natural spoken Sinhala only (Sinhala script + Latin for names/brands)\n"
            "- glossary_updates: only recurring character names or key terms from this batch\n"
            "- No empty translations, no reasoning, no source text echoed"
        )
        prompt = base_prompt

        # Use only the explicitly selected model. Each request is bounded to
        # 10 complete rounds; each configured key receives exactly 2 attempts
        # per round. A successful call returns immediately.
        model_name = self.model
        expected_ids = {x.number for x in batch}
        last_exc: Exception | None = None
        max_rounds = 10
        attempts_per_key = 2
        if key_indices is not None:
            key_order = list(key_indices)
        else:
            start = self._key_index % max(1, len(self.clients))
            key_order = list(range(start, len(self.clients))) + list(range(0, start))
        key_order = [i for i in key_order if 0 <= i < len(self.clients) and self.clients[i] is not None]
        if not key_order:
            key_order = [i for i, client in enumerate(self.clients) if client is not None]
        if not key_order:
            raise RuntimeError("No Gemini API keys are configured")

        # In parallel modes, all active lanes share a success epoch. Any batch
        # success resets the consecutive all-keys-failed round counter for the
        # remaining active batches, as requested. Each batch still returns
        # immediately when its own translation succeeds.
        if rotation_state is None:
            rotation_state = {"success_epoch": 0}
        observed_success_epoch = rotation_state.get("success_epoch", 0)
        failed_rounds = 0
        while failed_rounds < max_rounds:
            current_epoch = rotation_state.get("success_epoch", 0)
            if current_epoch != observed_success_epoch:
                observed_success_epoch = current_epoch
                failed_rounds = 0
                _live_log(
                    f"ROUND_RESET reason=parallel_batch_success epoch={observed_success_epoch}; "
                    "consecutive failed rounds reset to 0/10"
                )
            round_index = failed_rounds
            round_epoch = rotation_state.get("success_epoch", 0)
            _live_log(
                f"ROUND_START round={round_index + 1}/{max_rounds} "
                f"model={model_name} keys={len(key_order)} attempts_per_key={attempts_per_key}"
            )
            reset_due_to_peer_success = False
            # Visit every configured key once in each consecutive-failure round.
            for key_index in key_order:
                if key_indices is None:
                    self._key_index = key_index
                client = self.clients[key_index]
                if client is None:
                    continue
                key_number = key_index + 1

                for attempt in range(attempts_per_key):
                    t0 = time.monotonic()
                    _live_log(
                        f"START model={model_name} key={key_number}/{len(self.clients)} "
                        f"round={round_index + 1}/{max_rounds} "
                        f"attempt={attempt + 1}/{attempts_per_key} subs={len(batch)} "
                        f"pressure={_pressure.consecutive_503}"
                    )
                    try:
                        cfg: dict = {
                            "system_instruction": TRANSLATION_SYSTEM_PROMPT,
                            "response_mime_type": "application/json",
                            "max_output_tokens": 20000,
                        }
                        if model_name.startswith("gemini-3."):
                            cfg["thinking_config"] = types.ThinkingConfig(thinking_level="low")

                        response = await self._cancellable_api_call(
                            asyncio.to_thread(
                                client.models.generate_content,
                                model=model_name,
                                contents=prompt,
                                config=types.GenerateContentConfig(**cfg),
                            ),
                            cancel_event,
                        )

                        raw = getattr(response, "text", None) or ""
                        if not raw:
                            raise ValueError("Gemini returned an empty response")
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            raise TranslationPayloadError(f"Invalid JSON from Gemini: {e.msg}") from e
                        if not isinstance(data, dict):
                            raise TranslationPayloadError("Gemini JSON root is not an object")
                        rows = data.get("results")
                        if not isinstance(rows, list):
                            raise TranslationPayloadError("Gemini JSON missing results array")

                        translations: dict[int, str] = {}
                        for row in rows:
                            if not isinstance(row, dict):
                                raise TranslationPayloadError("Non-object in results array")
                            raw_id = row.get("id")
                            try:
                                sid = int(raw_id)
                            except (TypeError, ValueError):
                                raise TranslationPayloadError(f"Invalid subtitle ID: {raw_id!r}")
                            txt = row.get("translation") or row.get("text")
                            if not isinstance(txt, str) or not normalize_text(txt):
                                raise TranslationPayloadError(f"Empty translation for ID {sid}")
                            txt = normalize_text(txt)
                            if not relaxed_script and self._bad_script(txt):
                                raise TranslationPayloadError(f"ID {sid}: not in Sinhala script")
                            if sid in translations:
                                raise TranslationPayloadError(f"Duplicate ID {sid}")
                            translations[sid] = txt

                        got = set(translations)
                        if got != expected_ids:
                            missing = sorted(expected_ids - got)
                            extra = sorted(got - expected_ids)
                            if missing:
                                raise TranslationPayloadError(
                                    f"Wrong IDs — missing={missing}, extra={extra}"
                                )
                            for eid in extra:
                                translations.pop(eid, None)
                            _live_log(f"EXTRA_IDS_IGNORED {len(extra)} bonus rows dropped")

                        gu = data.get("glossary_updates", {})
                        if not isinstance(gu, dict):
                            gu = {}
                        gu = {
                            str(k): str(v) for k, v in gu.items()
                            if str(k).strip() and str(v).strip()
                        }

                        _pressure.success()
                        self.last_used_model = model_name
                        if key_indices is None:
                            self._key_index = key_index
                        rotation_state["success_epoch"] = rotation_state.get("success_epoch", 0) + 1
                        _live_log(
                            f"SUCCESS model={model_name} key={key_number}/{len(self.clients)} "
                            f"round={round_index + 1}/{max_rounds} subs={len(batch)} "
                            f"dur={time.monotonic() - t0:.1f}s"
                        )
                        return GeminiCallResult(
                            translations=translations,
                            glossary_updates=gu,
                            used_model=model_name,
                            used_key_index=key_number,
                        )

                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_exc = exc
                        if self._is_503(exc):
                            _pressure.hit()
                        _live_log(
                            f"FAIL model={model_name} key={key_number}/{len(self.clients)} "
                            f"round={round_index + 1}/{max_rounds} "
                            f"attempt={attempt + 1}/{attempts_per_key} "
                            f"dur={time.monotonic() - t0:.1f}s "
                            f"err={type(exc).__name__}: {str(exc)[:220]}"
                        )
                        if isinstance(exc, TranslationPayloadError) and attempt + 1 < attempts_per_key:
                            prompt = base_prompt + (
                                "\n\nSTRICT CORRECTION: previous response failed validation: "
                                + str(exc)
                                + ". Regenerate the full results array. Use natural Sri Lankan Sinhala "
                                "in Sinhala Unicode. Do not use other scripts except Latin for names."
                            )
                        # If a sibling batch succeeded while this request was in
                        # flight, restart the shared consecutive-failure counter.
                        if rotation_state.get("success_epoch", 0) != round_epoch:
                            reset_due_to_peer_success = True
                            break

                if reset_due_to_peer_success:
                    break

                # Rotate only after both attempts for this key have completed.
                pos = key_order.index(key_index)
                next_index = key_order[(pos + 1) % len(key_order)]
                if key_indices is None:
                    self._key_index = next_index
                _live_log(f"ROTATE key={key_number}->{next_index + 1}")

                if rotation_state.get("success_epoch", 0) != round_epoch:
                    reset_due_to_peer_success = True
                    break

            if reset_due_to_peer_success or rotation_state.get("success_epoch", 0) != round_epoch:
                observed_success_epoch = rotation_state.get("success_epoch", 0)
                failed_rounds = 0
                _live_log(
                    f"ROUND_RESET reason=parallel_batch_success epoch={observed_success_epoch}; "
                    "consecutive failed rounds reset to 0/10"
                )
                continue

            failed_rounds += 1
            if failed_rounds < max_rounds:
                # Short bounded pause between full consecutive-failure rounds.
                await self._cancellable_sleep(1.0, cancel_event)

        self._payload_sweep_done = False
        message = (
            f"Translation failed after {max_rounds} consecutive complete rounds without any batch success "
            f"({len(key_order)} available keys × {attempts_per_key} attempts per key per round) "
            f"using selected model {model_name}. Last error: {last_exc}"
        )
        if last_exc and self._is_quota(last_exc):
            raise GeminiQuotaExhaustedError(message) from last_exc
        if last_exc and self._is_503(last_exc):
            raise GeminiServiceUnavailableError(message) from last_exc
        raise GeminiBatchFailedError(message) from last_exc
