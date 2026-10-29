from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_SUBTITLES = {".srt", ".ass", ".ssa"}


@dataclass
class SubtitleItem:
    number: int
    start_ms: int
    end_ms: int
    text: str
    timestamp_line: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.()\[\]{}+\- ]+", "_", name, flags=re.UNICODE)
    return name[:180] or "subtitle"


def is_subtitle_name(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_SUBTITLES


def unique_path(directory: Path, filename: str) -> Path:
    base = directory / safe_name(filename)
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    for n in range(2, 10_000):
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not create a unique filename")


def _load_ass(path: Path):
    import pysubs2

    last_error: Exception | None = None
    for enc in (None, "utf-8-sig", "utf-8", "utf-16", "cp932", "cp1252"):
        try:
            return pysubs2.load(str(path), encoding=enc)
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Could not read subtitle file: {last_error}")


def strip_ass_override_tags(text: str) -> str:
    text = re.sub(r"\{[^}]*\}", "", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n")
    text = text.replace("\\h", " ")
    return text.strip()


def ass_to_srt(input_path: Path, output_path: Path) -> Path:
    subs = _load_ass(input_path)
    clean_events = []
    for event in subs:
        if getattr(event, "is_comment", False):
            continue
        event.text = strip_ass_override_tags(event.text)
        clean_events.append(event)
    subs.events = clean_events
    subs.save(str(output_path), format_="srt", encoding="utf-8")
    return output_path


def _read_text_with_fallbacks(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp932", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Subtitle text encoding is not supported")


def read_srt(path: Path) -> list[SubtitleItem]:
    raw = _read_text_with_fallbacks(path)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n{2,}", raw.strip())
    items: list[SubtitleItem] = []
    seen: set[int] = set()
    for block in blocks:
        lines = block.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        if len(lines) < 3:
            continue
        try:
            number = int(lines[0].strip())
        except ValueError as exc:
            raise ValueError(f"Invalid SRT subtitle number: {lines[0]!r}") from exc
        if number in seen:
            raise ValueError(f"Duplicate SRT subtitle number: {number}")
        timestamp_line = lines[1].strip()
        match = re.match(
            r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?$",
            timestamp_line,
        )
        if not match:
            raise ValueError(f"Invalid SRT timestamp: {timestamp_line!r}")
        g = [int(x) for x in match.groups()[:8]]
        start = ((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3]
        end = ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7]
        text = "\n".join(lines[2:])
        items.append(SubtitleItem(number, start, end, text, timestamp_line))
        seen.add(number)
    if not items:
        raise ValueError("No valid SRT subtitle items found")
    return items


def normalize_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()


def write_srt(items: list[SubtitleItem], path: Path) -> None:
    blocks: list[str] = []
    for item in items:
        timestamp = item.timestamp_line or f"{format_srt_time(item.start_ms)} --> {format_srt_time(item.end_ms)}"
        text = normalize_text(item.text)
        blocks.append(f"{item.number}\n{timestamp}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def format_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def make_output_path(input_path: Path, output_dir: Path) -> Path:
    return unique_path(output_dir, f"{input_path.stem}__si.srt")


def extract_zip(zip_path: Path, output_dir: Path, max_members: int, max_uncompressed_bytes: int) -> list[Path]:
    result: list[Path] = []
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and is_subtitle_name(i.filename)]
        if len(infos) > max_members:
            raise ValueError(f"ZIP has {len(infos)} subtitle files; maximum is {max_members}.")
        if not infos:
            raise ValueError("ZIP has no .srt/.ass/.ssa subtitle files.")
        for info in infos:
            member = Path(info.filename)
            if member.is_absolute() or ".." in member.parts:
                continue
            total_uncompressed += int(info.file_size)
            if total_uncompressed > max_uncompressed_bytes:
                raise ValueError("ZIP expands beyond the configured safety limit.")
            target = unique_path(output_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            result.append(target)
    return result


def validate_file_size(path: Path, max_bytes: int) -> None:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"File is larger than {max_bytes // (1024 * 1024)} MB.")


def detect_context_hint(filename: str, zip_name: str | None = None) -> str:
    raw = zip_name or filename
    stem = Path(raw).stem
    stem = re.sub(r"\[[^\]]*(1080p|720p|480p|2160p|WEB|WEB-DL|BluRay|BDRip|x264|x265|HEVC|AAC|FLAC)[^\]]*\]", " ", stem, flags=re.I)
    stem = re.sub(r"\bS\d{1,2}(?:E\d{1,4})?(?:[- ]?\d{1,3})?\b", " ", stem, flags=re.I)
    stem = re.sub(r"\b(?:episode|ep)\.?\s*\d{1,4}\b", " ", stem, flags=re.I)
    stem = re.sub(r"\b\d{1,4}\b", " ", stem)
    stem = re.sub(r"[_\.]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    return stem[:160]


def build_chunks(items: list[SubtitleItem], max_entries: int, max_chars: int) -> list[list[SubtitleItem]]:
    chunks: list[list[SubtitleItem]] = []
    current: list[SubtitleItem] = []
    current_chars = 0
    for item in items:
        cost = len(item.text) + 80
        if current and (len(current) >= max_entries or current_chars + cost > max_chars):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += cost
    if current:
        chunks.append(current)
    return chunks
