#!/usr/bin/env python3
"""Generate word-timed Edge narration and a deterministic FFmpeg training video.

This is the only supported production path for online Edge TTS. It deliberately
has no implicit local-TTS or native-video fallback: a dependency or service
failure stops the run, so voice, subtitle timing, rendering and QA cannot drift.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PIPELINE_VERSION = "2.0"
TICKS_PER_SECOND = 10_000_000
SENTENCE_END = frozenset("。！？!?")
CLAUSE_END = frozenset("，,；;：:")
CLOSING_PUNCTUATION = frozenset('”’」』）》)"\'')


@dataclass(frozen=True)
class SlideSpeech:
    number: int
    title: str
    text: str


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class MappedWord:
    start: float
    end: float
    source_start: int
    source_end: int


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def parse_speech(path: Path) -> list[SlideSpeech]:
    source = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^## Slide\s+(\d+):\s*(.+?)\s*$", source, re.MULTILINE))
    slides: list[SlideSpeech] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        body = source[match.end():end].split("\n---", 1)[0].strip()
        narration = "\n".join(line.strip() for line in body.splitlines() if line.strip())
        slides.append(SlideSpeech(int(match.group(1)), match.group(2).strip(), narration))
    numbers = [slide.number for slide in slides]
    if not slides or numbers != list(range(1, len(slides) + 1)):
        raise ValueError(f"speech.md must contain continuous Slide 1..N headings, got {numbers}")
    if any(not slide.text for slide in slides):
        raise ValueError("every slide must have non-empty narration")
    return slides


def find_ffmpeg(explicit: Path | None) -> Path:
    if explicit:
        resolved = explicit.expanduser().resolve()
        if resolved.is_file():
            return resolved
        raise FileNotFoundError(f"FFmpeg not found: {resolved}")
    system = shutil.which("ffmpeg")
    if system:
        return Path(system).resolve()
    try:
        import imageio_ffmpeg  # type: ignore

        return Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    except (ImportError, RuntimeError):
        raise FileNotFoundError(
            "FFmpeg not found; pass --ffmpeg or install ffmpeg/imageio-ffmpeg"
        ) from None


def edge_version() -> str:
    try:
        import edge_tts  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `edge-tts`; use a Python environment that contains edge-tts."
        ) from exc
    return importlib.metadata.version("edge-tts")


def normalize_rate(rate: str) -> str:
    value = rate.strip()
    if not value:
        return "+0%"
    if value.endswith("%"):
        return value if value[0] in "+-" else f"+{value}"
    if re.fullmatch(r"[+-]?\d+", value):
        return f"{int(value):+d}%"
    raise ValueError(f"invalid Edge rate: {rate!r}")


def text_key(text: str) -> str:
    return "".join(character.casefold() for character in text if character.isalnum())


def source_key_positions(text: str) -> tuple[str, list[int]]:
    key: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(text):
        if not character.isalnum():
            continue
        normalized = character.casefold()
        key.extend(normalized)
        positions.extend([index] * len(normalized))
    return "".join(key), positions


def map_word_boundaries(text: str, boundaries: list[dict[str, object]]) -> list[MappedWord]:
    source_key, positions = source_key_positions(text)
    boundary_keys = [text_key(str(boundary["text"])) for boundary in boundaries]
    if not source_key or source_key != "".join(boundary_keys):
        raise RuntimeError(
            "Edge word boundaries could not be aligned with speech.md; no audio/SRT pair was published."
        )
    mapped: list[MappedWord] = []
    key_offset = 0
    for boundary, word_key in zip(boundaries, boundary_keys):
        if not word_key:
            continue
        key_end = key_offset + len(word_key)
        start_ticks = int(boundary["offset"])
        duration_ticks = int(boundary["duration"])
        mapped.append(
            MappedWord(
                start=start_ticks / TICKS_PER_SECOND,
                end=(start_ticks + duration_ticks) / TICKS_PER_SECOND,
                source_start=positions[key_offset],
                source_end=positions[key_end - 1] + 1,
            )
        )
        key_offset = key_end
    return mapped


def trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def display_length(text: str, start: int, end: int) -> int:
    return sum(not character.isspace() for character in text[start:end])


def is_sentence_end(text: str, index: int) -> bool:
    character = text[index]
    if character in SENTENCE_END:
        return True
    if character != ".":
        return False
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    return not (previous.isdigit() and following.isdigit())


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        if not is_sentence_end(text, index):
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in CLOSING_PUNCTUATION:
            end += 1
        span = trim_span(text, start, end)
        if span[0] < span[1]:
            spans.append(span)
        start = end
        index = end
    span = trim_span(text, start, len(text))
    if span[0] < span[1]:
        spans.append(span)
    return spans


def hard_split_span(
    text: str,
    span: tuple[int, int],
    words: list[MappedWord],
    max_chars: int,
) -> list[tuple[int, int]]:
    start, end = span
    parts: list[tuple[int, int]] = []
    while display_length(text, start, end) > max_chars:
        remaining = display_length(text, start, end)
        part_count = (remaining + max_chars - 1) // max_chars
        target = (remaining + part_count - 1) // part_count
        candidates = [
            (word.source_end, display_length(text, start, word.source_end))
            for word in words
            if start < word.source_end < end
            and display_length(text, start, word.source_end) <= max_chars
        ]
        if candidates:
            split_at, _ = min(candidates, key=lambda item: (abs(item[1] - target), -item[1]))
        else:
            split_at = next(
                (word.source_end for word in words if start < word.source_end < end),
                end,
            )
        if split_at >= end:
            break
        part = trim_span(text, start, split_at)
        if part[0] < part[1]:
            parts.append(part)
        start = split_at
    part = trim_span(text, start, end)
    if part[0] < part[1]:
        parts.append(part)
    return parts


def split_sentence_span(
    text: str,
    sentence: tuple[int, int],
    words: list[MappedWord],
    max_chars: int,
) -> list[tuple[int, int]]:
    if display_length(text, *sentence) <= max_chars:
        return [sentence]
    start, end = sentence
    clauses: list[tuple[int, int]] = []
    clause_start = start
    for index in range(start, end):
        if text[index] not in CLAUSE_END:
            continue
        clause = trim_span(text, clause_start, index + 1)
        if clause[0] < clause[1]:
            clauses.append(clause)
        clause_start = index + 1
    clause = trim_span(text, clause_start, end)
    if clause[0] < clause[1]:
        clauses.append(clause)
    atoms = [part for clause in clauses for part in hard_split_span(text, clause, words, max_chars)]
    merged: list[tuple[int, int]] = []
    for atom in atoms:
        if not merged:
            merged.append(atom)
            continue
        candidate = (merged[-1][0], atom[1])
        if display_length(text, *candidate) <= max_chars:
            merged[-1] = candidate
        else:
            merged.append(atom)
    return merged


def subtitle_cues(
    text: str,
    boundaries: list[dict[str, object]],
    max_chars: int,
) -> list[SubtitleCue]:
    words = map_word_boundaries(text, boundaries)
    spans = [
        span
        for sentence in sentence_spans(text)
        for span in split_sentence_span(text, sentence, words, max_chars)
    ]
    pending: list[tuple[float, float, str]] = []
    assigned: list[int] = []
    for start, end in spans:
        matching = [
            (index, word)
            for index, word in enumerate(words)
            if word.source_start >= start and word.source_end <= end
        ]
        if not matching:
            continue
        cue_text = re.sub(r"\s+", " ", text[start:end]).strip()
        assigned.extend(index for index, _ in matching)
        pending.append((matching[0][1].start, matching[-1][1].end, cue_text))
    if assigned != list(range(len(words))) or not pending:
        raise RuntimeError("not every Edge word boundary was assigned to a subtitle cue")
    cues: list[SubtitleCue] = []
    for index, (start, word_end, cue_text) in enumerate(pending):
        next_start = pending[index + 1][0] if index + 1 < len(pending) else None
        end = next_start if next_start is not None and next_start > word_end else word_end
        if cues and start < cues[-1].end:
            if cues[-1].end - start > 0.10:
                raise RuntimeError("Edge returned overlapping word timings over 100 ms")
            start = cues[-1].end
        if end <= start:
            raise RuntimeError("Edge returned an invalid subtitle timing interval")
        cues.append(SubtitleCue(start, end, cue_text))
    if compact_text("".join(cue.text for cue in cues)) != compact_text(text):
        raise RuntimeError("generated subtitle text differs from speech.md")
    if max(display_length(cue.text, 0, len(cue.text)) for cue in cues) > max_chars:
        raise RuntimeError(f"generated subtitle cue exceeds {max_chars} characters")
    return cues


def srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(cues: list[SubtitleCue], path: Path) -> None:
    blocks = [
        f"{index}\n{srt_time(cue.start)} --> {srt_time(cue.end)}\n{cue.text}"
        for index, cue in enumerate(cues, 1)
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def srt_seconds(value: str) -> float:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def parse_srt(path: Path) -> list[SubtitleCue]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    pattern = re.compile(
        r"^(?:\d+\s*\n)?(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})\s*\n(?P<text>[\s\S]+)$"
    )
    cues: list[SubtitleCue] = []
    for block in re.split(r"\r?\n\s*\r?\n", raw):
        match = pattern.match(block.strip())
        if not match:
            raise ValueError(f"malformed SRT block in {path}: {block[:80]!r}")
        cue = SubtitleCue(
            srt_seconds(match.group("start")),
            srt_seconds(match.group("end")),
            "".join(part.strip() for part in match.group("text").splitlines()),
        )
        if cue.end <= cue.start or (cues and cue.start < cues[-1].end):
            raise ValueError(f"invalid or overlapping subtitle cue in {path}")
        cues.append(cue)
    if not cues:
        raise ValueError(f"no subtitle cues in {path}")
    return cues


def trim_leading_silence(cues: list[SubtitleCue], threshold: float = 2.0) -> list[SubtitleCue]:
    """Shift excessive Edge pre-roll out of the clip while preserving cue spacing."""
    if not cues or cues[0].start <= threshold:
        return cues
    shift = max(0.0, cues[0].start - 0.45)
    return [SubtitleCue(max(0.0, cue.start - shift), max(0.001, cue.end - shift), cue.text) for cue in cues]


def temporary_path(target: Path, suffix: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=suffix, dir=target.parent)
    os.close(descriptor)
    return Path(raw)


async def generate_edge_pair(
    slide: SlideSpeech,
    audio_path: Path,
    subtitle_path: Path,
    *,
    voice: str,
    rate: str,
    max_chars: int,
) -> None:
    import edge_tts

    staged_audio = temporary_path(audio_path, ".mp3")
    staged_srt = temporary_path(subtitle_path, ".srt")
    boundaries: list[dict[str, object]] = []
    received_audio = False
    try:
        first_pass = edge_tts.Communicate(
            slide.text, voice=voice, rate=normalize_rate(rate), boundary="WordBoundary"
        )
        with staged_audio.open("wb") as handle:
            async for chunk in first_pass.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                    received_audio = True
                elif chunk["type"] == "WordBoundary":
                    boundaries.append(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not received_audio or staged_audio.stat().st_size < 10_000:
            raise RuntimeError(f"Edge returned no valid audio for slide {slide.number}")
        write_srt(subtitle_cues(slide.text, boundaries, max_chars), staged_srt)
        if compact_text("".join(cue.text for cue in parse_srt(staged_srt))) != compact_text(slide.text):
            raise RuntimeError(f"staged subtitle differs from slide {slide.number} narration")
        os.replace(staged_audio, audio_path)
        os.replace(staged_srt, subtitle_path)
    finally:
        staged_audio.unlink(missing_ok=True)
        staged_srt.unlink(missing_ok=True)


def load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"pipeline_version": PIPELINE_VERSION, "slides": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("pipeline_version") != PIPELINE_VERSION:
        return {"pipeline_version": PIPELINE_VERSION, "slides": {}}
    return data


def write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = temporary_path(path, ".json")
    try:
        staged.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def reusable_pair(
    slide: SlideSpeech,
    audio: Path,
    srt: Path,
    entry: object,
    *,
    voice: str,
    rate: str,
    max_chars: int,
) -> bool:
    if not isinstance(entry, dict) or not audio.is_file() or not srt.is_file():
        return False
    expected = {
        "text_sha256": text_hash(slide.text),
        "voice": voice,
        "rate": normalize_rate(rate),
        "source_max_chars": max_chars,
        "audio_sha256": sha256(audio),
        "subtitle_sha256": sha256(srt),
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        return False
    try:
        return compact_text("".join(cue.text for cue in parse_srt(srt))) == compact_text(slide.text)
    except (OSError, ValueError):
        return False


async def generate_edge_media(
    project: Path,
    slides: list[SlideSpeech],
    *,
    voice: str,
    rate: str,
    concurrency: int,
    source_max_chars: int,
    resume: bool,
    output_tag: str,
    request_timeout_seconds: float,
) -> dict[str, object]:
    audio_dir = project / "audio"
    subtitle_dir = project / "subtitles"
    state_path = project / "qa" / f"tts_state_{output_tag}.json"
    state = load_state(state_path)
    state.update(
        {
            "pipeline_version": PIPELINE_VERSION,
            "provider": "edge",
            "voice": voice,
            "rate": normalize_rate(rate),
            "online_external_transfer_authorized": True,
            "sent_text_scope": "approved per-slide narration bodies from speech.md",
            "updated_at": now_iso(),
            "slides": state.get("slides", {}),
        }
    )
    slide_state = state["slides"]
    assert isinstance(slide_state, dict)
    semaphore = asyncio.Semaphore(concurrency)
    state_lock = asyncio.Lock()

    async def job(slide: SlideSpeech) -> None:
        stem = f"slide_{slide.number:02d}"
        audio = audio_dir / f"{stem}.mp3"
        srt = subtitle_dir / f"{stem}.srt"
        if resume and reusable_pair(
            slide,
            audio,
            srt,
            slide_state.get(stem),
            voice=voice,
            rate=rate,
            max_chars=source_max_chars,
        ):
            print(f"[TTS resume] {stem}", flush=True)
            return
        async with semaphore:
            last_error: BaseException | None = None
            for attempt in range(1, 4):
                try:
                    await asyncio.wait_for(
                        generate_edge_pair(
                            slide,
                            audio,
                            srt,
                            voice=voice,
                            rate=rate,
                            max_chars=source_max_chars,
                        ),
                        timeout=request_timeout_seconds,
                    )
                    last_error = None
                    break
                except BaseException as exc:  # keep a resumable, explicit stop after retries
                    last_error = exc
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
            if last_error is not None:
                raise RuntimeError(f"Edge generation failed for {stem} after 3 attempts") from last_error
        entry = {
            "text_sha256": text_hash(slide.text),
            "voice": voice,
            "rate": normalize_rate(rate),
            "source_max_chars": source_max_chars,
            "audio_sha256": sha256(audio),
            "subtitle_sha256": sha256(srt),
            "generated_at": now_iso(),
        }
        async with state_lock:
            slide_state[stem] = entry
            state["updated_at"] = now_iso()
            write_state(state_path, state)
        print(f"[TTS] {stem}", flush=True)

    results = await asyncio.gather(*(job(slide) for slide in slides), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise RuntimeError(
            f"TTS stopped with {len(failures)} failed slide(s); rerun with --resume after resolving the service error"
        ) from failures[0]
    write_state(state_path, state)
    return state


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "（").replace("}", "）")


def write_ass(
    cues: list[SubtitleCue],
    path: Path,
    *,
    title: str,
    font: str,
    font_size: int,
    display_max_chars: int,
) -> int:
    longest = max(display_length(cue.text, 0, len(cue.text)) for cue in cues)
    if longest > display_max_chars:
        raise ValueError(f"display subtitle exceeds {display_max_chars} characters: {longest}")
    events = [
        f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,{ass_escape(cue.text)}"
        for cue in cues
    ]
    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0,2,160,160,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return len(events)


def media_duration(ffmpeg: Path, path: Path) -> float:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"unable to determine media duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", value).strip(" ._")
    return cleaned or "policy"


def write_global_srt(
    slide_cues: list[list[SubtitleCue]],
    slide_durations: list[float],
    output: Path,
) -> int:
    lines: list[str] = []
    number = 1
    offset = 0.0
    for cues, duration in zip(slide_cues, slide_durations):
        for cue in cues:
            lines.extend(
                [
                    str(number),
                    f"{srt_time(offset + cue.start)} --> {srt_time(offset + cue.end)}",
                    cue.text,
                    "",
                ]
            )
            number += 1
        offset += duration
    output.write_text("\n".join(lines), encoding="utf-8")
    return number - 1


def check_inputs(project: Path, slides: list[SlideSpeech], *, skip_tts: bool) -> None:
    missing_images = [
        str(project / "origin_image" / f"slide_{slide.number:02d}.png")
        for slide in slides
        if not (project / "origin_image" / f"slide_{slide.number:02d}.png").is_file()
    ]
    if missing_images:
        raise FileNotFoundError("missing slide images:\n" + "\n".join(missing_images))
    if skip_tts:
        for slide in slides:
            for relative in (
                f"audio/slide_{slide.number:02d}.mp3",
                f"subtitles/slide_{slide.number:02d}.srt",
            ):
                if not (project / relative).is_file():
                    raise FileNotFoundError(f"missing reused media input: {relative}")


def compose_video(
    project: Path,
    slides: list[SlideSpeech],
    *,
    ffmpeg: Path,
    policy_id: str,
    title: str,
    voice: str,
    rate: str,
    font: str,
    font_file: Path,
    font_size: int,
    band_y: int,
    band_height: int,
    source_max_chars: int,
    display_max_chars: int,
    tail_seconds: float,
    output_tag: str,
    resume: bool,
    online_authorized: bool,
) -> dict[str, object]:
    subtitle_dir = project / "subtitles"
    video_dir = project / "video"
    work_dir = project / "qa" / f"media_work_{output_tag}"
    clip_dir = work_dir / "clips"
    ass_dir = work_dir / "ass"
    for directory in (video_dir, clip_dir, ass_dir):
        directory.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    all_cues: list[list[SubtitleCue]] = []
    durations: list[float] = []
    slide_hashes: dict[str, dict[str, str]] = {}
    display_counts: dict[str, int] = {}
    fonts_dir = font_file.parent.as_posix().replace("'", r"\'")

    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        image = project / "origin_image" / f"{stem}.png"
        audio = project / "audio" / f"{stem}.mp3"
        srt = subtitle_dir / f"{stem}.srt"
        ass = ass_dir / f"{stem}.ass"
        clip = clip_dir / f"{stem}.mp4"
        cues = parse_srt(srt)
        cues = trim_leading_silence(cues)
        if compact_text("".join(cue.text for cue in cues)) != compact_text(slide.text):
            raise RuntimeError(f"subtitle differs from speech.md for {stem}")
        display_counts[stem] = write_ass(
            cues,
            ass,
            title=f"{policy_id} {stem}",
            font=font,
            font_size=font_size,
            display_max_chars=display_max_chars,
        )
        target_duration = media_duration(ffmpeg, audio) + tail_seconds
        newest_input = max(image.stat().st_mtime, audio.stat().st_mtime, srt.stat().st_mtime, ass.stat().st_mtime)
        can_reuse_clip = resume and clip.is_file() and clip.stat().st_size > 10_000 and clip.stat().st_mtime >= newest_input
        if not can_reuse_clip:
            ass_path = ass.as_posix().replace("'", r"\'")
            filter_graph = (
                "scale=1920:1080:force_original_aspect_ratio=disable,"
                f"drawbox=x=0:y={band_y}:w=iw:h={band_height}:color=0x111827:t=fill,"
                f"ass='{ass_path}':fontsdir='{fonts_dir}'"
            )
            audio_filter = f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur={tail_seconds}"
            if parse_srt(srt)[0].start > 2.0:
                shift = parse_srt(srt)[0].start - 0.45
                audio_filter = (
                    f"atrim=start={shift:.3f},asetpts=PTS-STARTPTS,"
                    f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur={tail_seconds}"
                )
                target_duration -= shift
            run(
                [
                    str(ffmpeg), "-y", "-loop", "1", "-framerate", "30", "-i", str(image),
                    "-i", str(audio), "-vf", filter_graph, "-map", "0:v:0", "-map", "1:a:0",
                    "-af", audio_filter,
                    "-t", f"{target_duration:.3f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                    str(clip),
                ]
            )
            print(f"[Video] {stem}", flush=True)
        else:
            print(f"[Video resume] {stem}", flush=True)
        duration = media_duration(ffmpeg, clip)
        clips.append(clip)
        all_cues.append(cues)
        durations.append(duration)
        slide_hashes[stem] = {
            "speech_sha256": text_hash(slide.text),
            "image_sha256": sha256(image),
            "audio_sha256": sha256(audio),
            "subtitle_sha256": sha256(srt),
            "ass_sha256": sha256(ass),
            "clip_sha256": sha256(clip),
        }

    concat_list = clip_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    base = safe_filename(f"{policy_id}_{title}")
    final_video = video_dir / f"{base}_{output_tag}_字幕安全区培训视频.mp4"
    run(
        [
            str(ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart", str(final_video),
        ]
    )
    global_srt = subtitle_dir / f"{base}_{output_tag}_全程字幕.srt"
    cue_count = write_global_srt(all_cues, durations, global_srt)
    ffmpeg_version = run([str(ffmpeg), "-version"], capture=True).stdout.splitlines()[0]
    manifest: dict[str, object] = {
        "pipeline_name": "training-video-production locked media pipeline",
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": now_iso(),
        "policy_id": policy_id,
        "policy_title": title,
        "source_of_truth": "speech.md approved narration only",
        "fallback_policy": "fail_closed_no_tool_switch",
        "video": str(final_video.relative_to(project)),
        "global_subtitles": str(global_srt.relative_to(project)),
        "ass_dir": str(ass_dir.relative_to(project)),
        "clip_dir": str(clip_dir.relative_to(project)),
        "slide_count": len(slides),
        "slide_durations_seconds": {
            f"slide_{slide.number:02d}": round(duration, 3)
            for slide, duration in zip(slides, durations)
        },
        "duration_seconds": round(media_duration(ffmpeg, final_video), 3),
        "subtitle_cues": cue_count,
        "subtitle_timing_source": "Edge WordBoundary events from the same stream as each MP3",
        "display_cue_counts": display_counts,
        "slide_hashes": slide_hashes,
        "audio": {
            "generator": "bundled Edge word-timed generator",
            "provider": "Microsoft Edge online TTS",
            "edge_tts_version": edge_version(),
            "voice": voice,
            "rate": normalize_rate(rate),
            "online_external_transfer_authorized": online_authorized,
            "sent_text_scope": "approved per-slide narration bodies from speech.md",
            "loudness_target_lufs": -16,
            "true_peak_target_dbfs": -1.5,
        },
        "video_spec": {
            "composer": "FFmpeg only",
            "ffmpeg_version": ffmpeg_version,
            "resolution": "1920x1080",
            "fps": 30,
            "codec": "H.264 + AAC",
            "pixel_format": "yuv420p",
            "slide_mode": "full-bleed 16:9",
            "subtitle_band": {"y": band_y, "height": band_height},
            "subtitle_style": {
                "font": font,
                "font_file": str(font_file),
                "font_size_px": font_size,
                "max_lines": 1,
                "source_max_chars": source_max_chars,
                "max_display_chars": display_max_chars,
                "horizontal_margin_px": 160,
            },
        },
    }
    manifest_path = project / "qa" / f"media_manifest_{output_tag}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--provider", choices=("edge",), default="edge")
    parser.add_argument("--authorize-online-tts", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--rate", default="-5%")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--subtitle-source-max-chars", type=int, default=20)
    parser.add_argument("--subtitle-display-max-chars", type=int, default=20)
    parser.add_argument("--subtitle-font", default="Heiti SC")
    parser.add_argument(
        "--subtitle-font-file",
        type=Path,
        default=Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    )
    parser.add_argument("--subtitle-font-size", type=int, default=44)
    parser.add_argument("--subtitle-band-y", type=int, default=990)
    parser.add_argument("--subtitle-band-height", type=int, default=90)
    parser.add_argument("--tail-seconds", type=float, default=0.55)
    parser.add_argument("--output-tag", default="V2")
    parser.add_argument("--tts-request-timeout", type=float, default=180.0)
    args = parser.parse_args()

    if args.concurrency < 1 or args.subtitle_source_max_chars < 1 or args.tts_request_timeout <= 0:
        parser.error("concurrency and subtitle character limits must be positive")
    if args.subtitle_display_max_chars < args.subtitle_source_max_chars:
        parser.error("display subtitle limit cannot be lower than source subtitle limit")
    if args.subtitle_band_y != 990 or args.subtitle_band_height != 90:
        parser.error("production subtitle band is locked to y=990 and height=90")
    if args.subtitle_font_size != 44:
        parser.error("production subtitle font size is locked to 44px")
    output_tag = safe_filename(args.output_tag)
    project = args.project_dir.expanduser().resolve()
    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    policy_id = str(manifest.get("policy_id", "")).strip()
    title = str(manifest.get("policy_title", "")).strip()
    if not policy_id or not title:
        raise ValueError("manifest.json must contain policy_id and policy_title")
    slides = parse_speech(project / "speech.md")
    check_inputs(project, slides, skip_tts=args.skip_tts)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    font_file = args.subtitle_font_file.expanduser().resolve()
    if not font_file.is_file():
        raise FileNotFoundError(f"subtitle font file not found: {font_file}")
    tts_version = edge_version()

    if args.check_only:
        print(
            f"PASS: locked pipeline {PIPELINE_VERSION}; {len(slides)} slides; "
            f"edge-tts={tts_version}; FFmpeg={ffmpeg}; font={font_file}"
        )
        print("NOTICE: full narration is sent only when --authorize-online-tts is present.")
        return 0

    if not args.skip_tts and not args.authorize_online_tts:
        raise SystemExit(
            "REFUSED: Edge TTS sends the approved narration to Microsoft online services. "
            "Obtain authorization for the current material, then add --authorize-online-tts."
        )
    if not args.skip_tts:
        asyncio.run(
            generate_edge_media(
                project,
                slides,
                voice=args.voice,
                rate=args.rate,
                concurrency=args.concurrency,
                source_max_chars=args.subtitle_source_max_chars,
                resume=args.resume,
                output_tag=output_tag,
                request_timeout_seconds=args.tts_request_timeout,
            )
        )
        online_authorized = True
    else:
        state_path = project / "qa" / f"tts_state_{output_tag}.json"
        state = load_state(state_path)
        online_authorized = (
            state.get("provider") == "edge"
            and state.get("voice") == args.voice
            and state.get("rate") == normalize_rate(args.rate)
            and state.get("online_external_transfer_authorized") is True
        )
        if not online_authorized:
            raise RuntimeError(
                "reused Edge media has no matching authorization/provenance state; rerun TTS explicitly"
            )

    output = compose_video(
        project,
        slides,
        ffmpeg=ffmpeg,
        policy_id=policy_id,
        title=title,
        voice=args.voice,
        rate=args.rate,
        font=args.subtitle_font,
        font_file=font_file,
        font_size=args.subtitle_font_size,
        band_y=args.subtitle_band_y,
        band_height=args.subtitle_band_height,
        source_max_chars=args.subtitle_source_max_chars,
        display_max_chars=args.subtitle_display_max_chars,
        tail_seconds=args.tail_seconds,
        output_tag=output_tag,
        resume=args.resume,
        online_authorized=online_authorized,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
