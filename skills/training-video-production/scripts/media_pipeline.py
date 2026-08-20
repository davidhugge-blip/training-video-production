#!/usr/bin/env python3
"""Generate aligned local narration and a deterministic FFmpeg training video.

MeloTTS plus MFA is the default local production path. The explicit Edge branch
remains available for currently authorized online use. The pipeline deliberately
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
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from melo_mfa_voiceover import (
    LOUDNESS as MELO_LOUDNESS,
    MELO_DEFAULT_SPEED,
    MELO_OFFICIAL_SPEAKERS,
    alignment_units,
    materialize_alignment_input,
    normalize_two_pass,
    parse_textgrid_words,
    run_melo_batch,
    run_melo_synthesis,
    run_mfa_alignment,
    scan_blocking_terms,
)


PIPELINE_VERSION = "4.1"
DEFAULT_TTS_PROVIDER = "melo"
DEFAULT_OUTPUT_TAG = "V4"
NATIVE_DECK_MODE = "native_generation"
EXISTING_PPT_DECK_MODE = "existing_finished_ppt"
DECK_MODES = (NATIVE_DECK_MODE, EXISTING_PPT_DECK_MODE)
TICKS_PER_SECOND = 10_000_000
SENTENCE_END = frozenset("。！？!?")
STRONG_CLAUSE_END = frozenset("；;：:")
WEAK_CLAUSE_END = frozenset("，,、")
CLAUSE_END = STRONG_CLAUSE_END | WEAK_CLAUSE_END
CLOSING_PUNCTUATION = frozenset('”’」』）》)"\'')
OPENING_PUNCTUATION = frozenset('“‘「『《（(【[')
LEFT_BOUND_PUNCTUATION = SENTENCE_END | CLAUSE_END | CLOSING_PUNCTUATION | frozenset("%％")
PAIRED_PUNCTUATION = {
    "《": "》",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "【": "】",
    "[": "]",
}
SEMANTIC_CONNECTORS = (
    "首先",
    "其次",
    "先",
    "再",
    "但是",
    "但",
    "因此",
    "所以",
    "同时",
    "以及",
    "并且",
    "另外",
    "最后",
    "然后",
    "需要",
    "必须",
    "不得",
)
DANGLING_CONNECTOR_SUFFIXES = (
    "首先",
    "其次",
    "最后",
    "先",
    "再",
    "但",
    "并",
    "和",
    "与",
    "或",
    "及",
)
PROTECTED_TOKEN_PATTERN = re.compile(
    r"[A-Z0-9]+(?:-[A-Z0-9]+){2,}"
    r"|\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?"
    r"|\d+(?:\.\d+)?(?:%|％|元|万元|天|日|月|年|次|分|小时|分钟|个|项|条|户)"
    r"|[\u4e00-\u9fff]{2,24}(?:管理办法|考核细则|管理制度|实施细则|管理规定|操作规程)"
    r"|[\u4e00-\u9fff]{1,12}(?:负责人|总经理|副总经理|执行总裁|副总裁|总裁|经理|主管|部门|中心)"
)


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


@dataclass(frozen=True)
class CanvasSpec:
    deck_mode: str
    source_width: int
    source_height: int
    slide_width: int
    slide_height: int
    video_width: int
    video_height: int
    band_y: int
    band_height: int
    font_size: int
    target_width_px: int
    hard_width_px: int
    horizontal_margin_px: int
    vertical_margin_px: int
    outline_px: float
    vertical_padding_px: int

    @property
    def resolution(self) -> str:
        return f"{self.video_width}x{self.video_height}"

    @property
    def comparison_height(self) -> int:
        return self.band_y if self.deck_mode == NATIVE_DECK_MODE else self.slide_height


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


def record_local_voice_selection(
    project: Path,
    manifest: dict[str, object],
    *,
    speaker: str,
    speed: float,
    confirmation_basis: str,
    output_tag: str = DEFAULT_OUTPUT_TAG,
) -> None:
    approvals = manifest.setdefault("approvals", {})
    if not isinstance(approvals, dict):
        raise ValueError("manifest.approvals must be an object")
    voiceover = manifest.setdefault("voiceover", {})
    if not isinstance(voiceover, dict):
        raise ValueError("manifest.voiceover must be an object")
    approvals["local_voice_selection"] = "approved"
    voiceover.update(
        {
            "default_provider": DEFAULT_TTS_PROVIDER,
            "selected_provider": "melo",
            "selected_voice": speaker,
            "speed": speed,
            "voice_confirmation_basis": confirmation_basis,
            "voice_cloning": False,
            "online_fallback": False,
            "output_tag": output_tag,
        }
    )
    manifest["updated_at"] = now_iso()
    (project / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def make_text_measurer(font_file: Path, font_size: int) -> Callable[[str], float]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required for semantic subtitle width measurement") from exc
    font = ImageFont.truetype(str(font_file), font_size)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1), "black"))

    def measure(text: str) -> float:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        return float(right - left)

    return measure


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


def protected_spans(
    text: str,
    *,
    measure_text: Callable[[str], float],
    hard_width_px: int,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stacks: dict[str, list[int]] = {opening: [] for opening in PAIRED_PUNCTUATION}
    closing_to_opening = {closing: opening for opening, closing in PAIRED_PUNCTUATION.items()}
    for index, character in enumerate(text):
        if character in stacks:
            stacks[character].append(index)
        elif character in closing_to_opening:
            opening = closing_to_opening[character]
            if stacks[opening]:
                start = stacks[opening].pop()
                end = index + 1
                if measure_text(text[start:end]) <= hard_width_px:
                    spans.append((start, end))
    spans.extend(
        (match.start(), match.end())
        for match in PROTECTED_TOKEN_PATTERN.finditer(text)
        if measure_text(match.group(0)) <= hard_width_px
    )
    return sorted(set(spans))


def normalized_break_position(text: str, position: int, end: int) -> int:
    while position < end and text[position] in LEFT_BOUND_PUNCTUATION:
        position += 1
    return position


def boundary_priority(text: str, position: int, end: int) -> int:
    if position >= end:
        return 0
    previous = text[position - 1] if position else ""
    if previous in SENTENCE_END:
        return 0
    if previous in STRONG_CLAUSE_END:
        return 1
    if previous in WEAK_CLAUSE_END:
        return 2
    following = text[position:].lstrip()
    if any(following.startswith(connector) for connector in SEMANTIC_CONNECTORS):
        return 3
    return 4


def words_in_span(words: list[MappedWord], start: int, end: int) -> list[MappedWord]:
    return [word for word in words if word.source_start >= start and word.source_end <= end]


def span_duration(words: list[MappedWord], start: int, end: int) -> float:
    matching = words_in_span(words, start, end)
    return matching[-1].end - matching[0].start if matching else 0.0


def partition_span(
    text: str,
    span: tuple[int, int],
    words: list[MappedWord],
    *,
    measure_text: Callable[[str], float],
    max_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> list[tuple[int, int]] | None:
    start, end = span
    protected = protected_spans(
        text[start:end], measure_text=measure_text, hard_width_px=hard_width_px
    )
    protected = [(left + start, right + start) for left, right in protected]
    positions = {start, end}
    for word in words:
        if not (start < word.source_end < end):
            continue
        position = normalized_break_position(text, word.source_end, end)
        if position >= end:
            positions.add(end)
            continue
        if any(left < position < right for left, right in protected):
            continue
        if text[position - 1] in OPENING_PUNCTUATION or text[position] in LEFT_BOUND_PUNCTUATION:
            continue
        positions.add(position)
    ordered = sorted(positions)
    best: dict[int, tuple[float, list[tuple[int, int]]]] = {start: (0.0, [])}
    for right_index in range(1, len(ordered)):
        right = ordered[right_index]
        for left in ordered[:right_index]:
            if left not in best:
                continue
            segment = trim_span(text, left, right)
            if segment[0] >= segment[1]:
                continue
            matching = words_in_span(words, *segment)
            if not matching:
                continue
            rendered = re.sub(r"\s+", " ", text[segment[0]:segment[1]]).strip()
            width = measure_text(rendered)
            duration = matching[-1].end - matching[0].start
            if width > max_width_px or duration > preferred_max_seconds + 0.05:
                continue
            short_penalty = 0.0
            if duration < 1.0:
                short_penalty += 500.0
            if display_length(text, *segment) <= 3:
                short_penalty += 300.0
            semantic_penalty = boundary_priority(text, right, end) * 55.0
            fill_penalty = max_width_px - width
            score = best[left][0] + 1000.0 + semantic_penalty + short_penalty + fill_penalty / 100.0
            if right not in best or score < best[right][0]:
                best[right] = (score, [*best[left][1], segment])
    return best.get(end, (0.0, []))[1] or None


def semantic_spans(
    text: str,
    words: list[MappedWord],
    *,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for sentence in sentence_spans(text):
        parts = partition_span(
            text,
            sentence,
            words,
            measure_text=measure_text,
            max_width_px=target_width_px,
            hard_width_px=hard_width_px,
            preferred_max_seconds=preferred_max_seconds,
        )
        if parts is None:
            parts = partition_span(
                text,
                sentence,
                words,
                measure_text=measure_text,
                max_width_px=hard_width_px,
                hard_width_px=hard_width_px,
                preferred_max_seconds=preferred_max_seconds,
            )
        if parts is None:
            raise RuntimeError(
                "subtitle sentence cannot be segmented within the approved width and timing limits: "
                f"{text[sentence[0]:sentence[1]]!r}"
            )
        spans.extend(parts)
    return spans


def cues_from_mapped_words(
    text: str,
    words: list[MappedWord],
    *,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> list[SubtitleCue]:
    spans = semantic_spans(
        text,
        words,
        measure_text=measure_text,
        target_width_px=target_width_px,
        hard_width_px=hard_width_px,
        preferred_max_seconds=preferred_max_seconds,
    )
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
        raise RuntimeError("not every timed speech unit was assigned to a subtitle cue")
    cues: list[SubtitleCue] = []
    for index, (start, word_end, cue_text) in enumerate(pending):
        next_start = pending[index + 1][0] if index + 1 < len(pending) else None
        end = next_start if next_start is not None and next_start > word_end else word_end
        if cues and start < cues[-1].end:
            if cues[-1].end - start > 0.10:
                raise RuntimeError("speech timing units overlap by more than 100 ms")
            start = cues[-1].end
        if end <= start:
            raise RuntimeError("speech timing produced an invalid subtitle interval")
        cues.append(SubtitleCue(start, end, cue_text))
    if compact_text("".join(cue.text for cue in cues)) != compact_text(text):
        raise RuntimeError("generated subtitle text differs from speech.md")
    if any(measure_text(cue.text) > hard_width_px for cue in cues):
        raise RuntimeError(f"generated subtitle cue exceeds {hard_width_px}px")
    return cues


def subtitle_cues(
    text: str,
    boundaries: list[dict[str, object]],
    *,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> list[SubtitleCue]:
    words = map_word_boundaries(text, boundaries)
    return cues_from_mapped_words(
        text,
        words,
        measure_text=measure_text,
        target_width_px=target_width_px,
        hard_width_px=hard_width_px,
        preferred_max_seconds=preferred_max_seconds,
    )


def reflow_existing_cues(
    cues: list[SubtitleCue],
    *,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> list[SubtitleCue]:
    text_parts: list[str] = []
    source_ranges: list[list[int]] = []
    cursor = 0
    for cue in cues:
        text_parts.append(cue.text)
        end = cursor + len(cue.text)
        timed_start = cursor
        while timed_start < end and text_parts[-1][timed_start - cursor] in LEFT_BOUND_PUNCTUATION:
            timed_start += 1
        source_ranges.append([timed_start, end])
        cursor = end
    for index in range(len(cues) - 1):
        current = cues[index].text.rstrip()
        following = cues[index + 1].text.lstrip()
        if not following or following[0] in LEFT_BOUND_PUNCTUATION:
            continue
        for connector in DANGLING_CONNECTOR_SUFFIXES:
            if current.endswith(connector) and len(compact_text(current)) > len(connector):
                connector_start = source_ranges[index][1] - (
                    len(cues[index].text) - len(current)
                ) - len(connector)
                source_ranges[index][1] = connector_start
                source_ranges[index + 1][0] = min(
                    source_ranges[index + 1][0], connector_start
                )
                break
    full_text = "".join(text_parts)
    units = [
        MappedWord(cue.start, cue.end, source_start, source_end)
        for cue, (source_start, source_end) in zip(cues, source_ranges)
        if source_start < source_end
        and any(character.isalnum() for character in full_text[source_start:source_end])
    ]
    return cues_from_mapped_words(
        full_text,
        units,
        measure_text=measure_text,
        target_width_px=target_width_px,
        hard_width_px=hard_width_px,
        preferred_max_seconds=preferred_max_seconds,
    )


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
    boundary_path: Path,
    *,
    voice: str,
    rate: str,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> None:
    import edge_tts

    staged_audio = temporary_path(audio_path, ".mp3")
    staged_srt = temporary_path(subtitle_path, ".srt")
    staged_boundaries = temporary_path(boundary_path, ".json")
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
        write_srt(
            subtitle_cues(
                slide.text,
                boundaries,
                measure_text=measure_text,
                target_width_px=target_width_px,
                hard_width_px=hard_width_px,
                preferred_max_seconds=preferred_max_seconds,
            ),
            staged_srt,
        )
        staged_boundaries.write_text(
            json.dumps(boundaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if compact_text("".join(cue.text for cue in parse_srt(staged_srt))) != compact_text(slide.text):
            raise RuntimeError(f"staged subtitle differs from slide {slide.number} narration")
        os.replace(staged_audio, audio_path)
        os.replace(staged_srt, subtitle_path)
        boundary_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_boundaries, boundary_path)
    finally:
        staged_audio.unlink(missing_ok=True)
        staged_srt.unlink(missing_ok=True)
        staged_boundaries.unlink(missing_ok=True)


def load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"pipeline_version": PIPELINE_VERSION, "slides": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("pipeline_version") != PIPELINE_VERSION:
        return {"pipeline_version": PIPELINE_VERSION, "slides": {}}
    return data


def authorized_reuse_state(
    project: Path,
    slides: list[SlideSpeech],
    path: Path,
    *,
    voice: str,
    rate: str,
) -> bool:
    if not path.is_file():
        return False
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("provider") != "edge"
        or state.get("voice") != voice
        or state.get("rate") != normalize_rate(rate)
        or state.get("online_external_transfer_authorized") is not True
        or not isinstance(state.get("slides"), dict)
    ):
        return False
    entries = state["slides"]
    assert isinstance(entries, dict)
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        entry = entries.get(stem)
        audio = project / "audio" / f"{stem}.mp3"
        if (
            not isinstance(entry, dict)
            or not audio.is_file()
            or entry.get("text_sha256") != text_hash(slide.text)
            or entry.get("audio_sha256") != sha256(audio)
        ):
            return False
    return True


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
    boundary_path: Path,
    entry: object,
    *,
    voice: str,
    rate: str,
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> bool:
    if (
        not isinstance(entry, dict)
        or not audio.is_file()
        or not srt.is_file()
        or not boundary_path.is_file()
    ):
        return False
    expected = {
        "text_sha256": text_hash(slide.text),
        "voice": voice,
        "rate": normalize_rate(rate),
        "subtitle_target_width_px": target_width_px,
        "subtitle_hard_width_px": hard_width_px,
        "subtitle_preferred_max_seconds": preferred_max_seconds,
        "audio_sha256": sha256(audio),
        "subtitle_sha256": sha256(srt),
        "word_boundaries_sha256": sha256(boundary_path),
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
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
    resume: bool,
    output_tag: str,
    request_timeout_seconds: float,
) -> dict[str, object]:
    audio_dir = project / "audio"
    subtitle_dir = project / "subtitles"
    boundary_dir = project / "qa" / f"word_boundaries_{output_tag}"
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
        boundary_path = boundary_dir / f"{stem}.json"
        if resume and reusable_pair(
            slide,
            audio,
            srt,
            boundary_path,
            slide_state.get(stem),
            voice=voice,
            rate=rate,
            target_width_px=target_width_px,
            hard_width_px=hard_width_px,
            preferred_max_seconds=preferred_max_seconds,
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
                            boundary_path,
                            voice=voice,
                            rate=rate,
                            measure_text=measure_text,
                            target_width_px=target_width_px,
                            hard_width_px=hard_width_px,
                            preferred_max_seconds=preferred_max_seconds,
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
            "subtitle_target_width_px": target_width_px,
            "subtitle_hard_width_px": hard_width_px,
            "subtitle_preferred_max_seconds": preferred_max_seconds,
            "audio_sha256": sha256(audio),
            "subtitle_sha256": sha256(srt),
            "word_boundaries_sha256": sha256(boundary_path),
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


def write_local_term_gate(
    project: Path,
    slides: list[SlideSpeech],
    *,
    output_tag: str,
) -> tuple[Path, dict[str, object]]:
    entries: dict[str, object] = {}
    blocking_count = 0
    for slide in slides:
        terms = scan_blocking_terms(slide.text)
        blocking_count += len(terms)
        entries[f"slide_{slide.number:02d}"] = {
            "title": slide.title,
            "blocking_terms": terms,
            "text_sha256": text_hash(slide.text),
        }
    report: dict[str, object] = {
        "generated_at": now_iso(),
        "provider": "MeloTTS local ZH",
        "status": "passed" if blocking_count == 0 else "blocked_pending_approved_natural_chinese_rewrite",
        "blocking_rule": "Latin letters and Arabic-number expressions cannot be sent directly to MeloTTS production synthesis",
        "blocking_term_count": blocking_count,
        "slides": entries,
        "manual_pronunciation_gate": (
            "rare names and uncommon Chinese terms still require listening review; "
            "the automated scan does not approve their pronunciation"
        ),
        "subtitle_rule": "speech.md remains the exact displayed subtitle source",
    }
    path = project / "qa" / f"voiceover_term_gate_{output_tag}.json"
    write_state(path, report)
    return path, report


def local_reuse_state(
    project: Path,
    slides: list[SlideSpeech],
    path: Path,
    *,
    speaker: str,
    speed: float,
    output_tag: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("pipeline_version") != PIPELINE_VERSION
        or state.get("provider") != "melo"
        or state.get("speaker") != speaker
        or state.get("speed") != speed
        or state.get("local_only") is not True
        or state.get("online_external_transfer_authorized") is not False
        or state.get("voice_confirmed") is not True
        or not isinstance(state.get("slides"), dict)
    ):
        return None
    entries = state["slides"]
    assert isinstance(entries, dict)
    alignment_dir = project / "qa" / f"mfa_alignments_{output_tag}"
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        entry = entries.get(stem)
        audio = project / "audio" / f"{stem}.wav"
        srt = project / "subtitles" / f"{stem}.srt"
        alignment = alignment_dir / f"{stem}.json"
        if (
            not isinstance(entry, dict)
            or not audio.is_file()
            or not srt.is_file()
            or not alignment.is_file()
            or entry.get("text_sha256") != text_hash(slide.text)
            or entry.get("audio_sha256") != sha256(audio)
            or entry.get("subtitle_sha256") != sha256(srt)
            or entry.get("alignment_sha256") != sha256(alignment)
        ):
            return None
        try:
            if compact_text("".join(cue.text for cue in parse_srt(srt))) != compact_text(slide.text):
                return None
        except (OSError, ValueError):
            return None
    return state


def mfa_version(mfa: Path, mfa_root: Path | None = None) -> str:
    environment = os.environ.copy()
    environment["PATH"] = f"{mfa.parent}{os.pathsep}{environment.get('PATH', '')}"
    if mfa_root is not None:
        environment["MFA_ROOT_DIR"] = str(mfa_root)
    result = subprocess.run(
        [str(mfa), "version"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return result.stdout.strip()


def generate_melo_preview(
    project: Path,
    *,
    ffmpeg: Path,
    melo_python: Path,
    melo_source: Path | None,
    speaker: str,
    speed: float,
    output_tag: str,
) -> dict[str, object]:
    preview_text = (
        "完成逐页语音合成之后，先执行质量检查，再核对音频、字幕和时间轴是否来自同一音频流。"
        "全部试听通过后，再进入人工复核。"
    )
    directory = project / "qa" / "voice_previews"
    directory.mkdir(parents=True, exist_ok=True)
    text_file = directory / f"melo_{speaker}_{output_tag}.txt"
    raw = directory / f"melo_{speaker}_{output_tag}_raw.wav"
    output = directory / f"melo_{speaker}_{output_tag}.wav"
    text_file.write_text(preview_text + "\n", encoding="utf-8")
    run_melo_synthesis(
        python=melo_python,
        helper_script=Path(__file__).with_name("melo_mfa_voiceover.py"),
        text_file=text_file,
        output=raw,
        speaker=speaker,
        speed=speed,
        melo_source=melo_source,
    )
    normalization = normalize_two_pass(ffmpeg, raw, output)
    manifest: dict[str, object] = {
        "generated_at": now_iso(),
        "provider": "MeloTTS local",
        "speaker": speaker,
        "speed": speed,
        "official_fixed_voice": speaker in MELO_OFFICIAL_SPEAKERS,
        "voice_cloning": False,
        "online": False,
        "preview_text": preview_text,
        "preview_text_sha256": text_hash(preview_text),
        "audio": str(output.relative_to(project)),
        "audio_sha256": sha256(output),
        "normalization": normalization,
        "status": "generated_pending_explicit_voice_confirmation",
    }
    write_state(directory / f"melo_{speaker}_{output_tag}.json", manifest)
    return manifest


def generate_melo_mfa_media(
    project: Path,
    slides: list[SlideSpeech],
    *,
    ffmpeg: Path,
    melo_python: Path,
    melo_source: Path | None,
    speaker: str,
    speed: float,
    voice_confirmation_basis: str,
    mfa: Path,
    mfa_root: Path,
    pkuseg_home: Path,
    cache_home: Path,
    mfa_dictionary: str,
    mfa_acoustic_model: str,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
    resume: bool,
    output_tag: str,
) -> dict[str, object]:
    if speaker not in MELO_OFFICIAL_SPEAKERS:
        raise RuntimeError(f"MeloTTS speaker must be one of {MELO_OFFICIAL_SPEAKERS}")
    for label, executable in (("Melo Python", melo_python), ("MFA", mfa)):
        if not executable.is_file():
            raise FileNotFoundError(f"{label} executable not found: {executable}")
    term_gate_path, term_gate = write_local_term_gate(project, slides, output_tag=output_tag)
    if term_gate["status"] != "passed":
        raise RuntimeError(
            f"local pronunciation gate blocked synthesis; review {term_gate_path} and approve "
            "natural-Chinese rewrites in speech.md before rerunning"
        )
    state_path = project / "qa" / f"tts_state_{output_tag}.json"
    if resume:
        reusable = local_reuse_state(
            project,
            slides,
            state_path,
            speaker=speaker,
            speed=speed,
            output_tag=output_tag,
        )
        if reusable is not None:
            print("[Local TTS resume] all MeloTTS WAV/SRT/MFA pairs verified", flush=True)
            return reusable

    raw_dir = project / "qa" / f"melo_raw_{output_tag}"
    text_dir = raw_dir / "text"
    audio_dir = project / "audio"
    subtitle_dir = project / "subtitles"
    corpus_dir = project / "qa" / f"mfa_corpus_{output_tag}"
    textgrid_dir = project / "qa" / f"mfa_textgrids_{output_tag}"
    alignment_dir = project / "qa" / f"mfa_alignments_{output_tag}"
    for directory in (
        raw_dir, text_dir, audio_dir, subtitle_dir, corpus_dir, textgrid_dir, alignment_dir
    ):
        directory.mkdir(parents=True, exist_ok=True)

    jobs: list[dict[str, str]] = []
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        text_file = text_dir / f"{stem}.txt"
        raw = raw_dir / f"{stem}.wav"
        text_file.write_text(slide.text + "\n", encoding="utf-8")
        jobs.append({"text_file": str(text_file), "output": str(raw)})
    jobs_file = raw_dir / "jobs.json"
    jobs_file.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_melo_batch(
        python=melo_python,
        helper_script=Path(__file__).with_name("melo_mfa_voiceover.py"),
        jobs_file=jobs_file,
        speaker=speaker,
        speed=speed,
        melo_source=melo_source,
    )

    normalizations: dict[str, object] = {}
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        raw = raw_dir / f"{stem}.wav"
        audio = audio_dir / f"{stem}.wav"
        normalizations[stem] = normalize_two_pass(ffmpeg, raw, audio)
        materialize_alignment_input(
            audio,
            corpus_dir / f"{stem}.lab",
            corpus_dir / f"{stem}.wav",
            slide.text,
        )
        print(f"[Local TTS] {stem}", flush=True)

    run_mfa_alignment(
        mfa=mfa,
        mfa_root=mfa_root,
        pkuseg_home=pkuseg_home,
        cache_home=cache_home,
        corpus_dir=corpus_dir,
        output_dir=textgrid_dir,
        dictionary=mfa_dictionary,
        acoustic_model=mfa_acoustic_model,
    )

    slide_state: dict[str, object] = {}
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        audio = audio_dir / f"{stem}.wav"
        srt = subtitle_dir / f"{stem}.srt"
        textgrid = textgrid_dir / f"{stem}.TextGrid"
        if not textgrid.is_file():
            raise RuntimeError(f"MFA did not export {textgrid}")
        duration, intervals = parse_textgrid_words(textgrid)
        expected = alignment_units(slide.text)
        actual = [str(interval["text"]) for interval in intervals]
        if actual != expected:
            raise RuntimeError(
                f"MFA label sequence differs from approved narration for {stem}; "
                "no subtitle was published"
            )
        boundaries = [
            {
                "text": interval["text"],
                "offset": round(float(interval["start"]) * TICKS_PER_SECOND),
                "duration": round(
                    (float(interval["end"]) - float(interval["start"])) * TICKS_PER_SECOND
                ),
            }
            for interval in intervals
        ]
        cues = subtitle_cues(
            slide.text,
            boundaries,
            measure_text=measure_text,
            target_width_px=target_width_px,
            hard_width_px=hard_width_px,
            preferred_max_seconds=preferred_max_seconds,
        )
        write_srt(cues, srt)
        if compact_text("".join(cue.text for cue in parse_srt(srt))) != compact_text(slide.text):
            raise RuntimeError(f"MFA subtitle differs from speech.md for {stem}")
        alignment_path = alignment_dir / f"{stem}.json"
        alignment_payload = {
            "generated_at": now_iso(),
            "method": "Montreal Forced Aligner actual-audio character-token alignment",
            "mfa_version": mfa_version(mfa, mfa_root),
            "dictionary": mfa_dictionary,
            "acoustic_model": mfa_acoustic_model,
            "audio": str(audio.relative_to(project)),
            "audio_sha256": sha256(audio),
            "speech_text_sha256": text_hash(slide.text),
            "textgrid": str(textgrid.relative_to(project)),
            "textgrid_sha256": sha256(textgrid),
            "duration_seconds": duration,
            "unit_count": len(intervals),
            "coverage": "exact",
            "intervals": intervals,
        }
        write_state(alignment_path, alignment_payload)
        slide_state[stem] = {
            "text_sha256": text_hash(slide.text),
            "speaker": speaker,
            "speed": speed,
            "audio_sha256": sha256(audio),
            "subtitle_sha256": sha256(srt),
            "alignment_sha256": sha256(alignment_path),
            "unit_count": len(intervals),
            "generated_at": now_iso(),
        }
        print(f"[MFA + SRT] {stem}", flush=True)

    state: dict[str, object] = {
        "pipeline_version": PIPELINE_VERSION,
        "provider": "melo",
        "speaker": speaker,
        "speed": speed,
        "voice_confirmed": True,
        "voice_confirmation_basis": voice_confirmation_basis,
        "voice_cloning": False,
        "local_only": True,
        "online_external_transfer_authorized": False,
        "network_fallback": False,
        "model_loading_policy": "offline cache only",
        "term_gate": str(term_gate_path.relative_to(project)),
        "term_gate_sha256": sha256(term_gate_path),
        "alignment_method": "MFA actual-audio forced alignment",
        "mfa_version": mfa_version(mfa, mfa_root),
        "mfa_dictionary": mfa_dictionary,
        "mfa_acoustic_model": mfa_acoustic_model,
        "normalization": {
            "target": MELO_LOUDNESS,
            "per_slide": normalizations,
        },
        "updated_at": now_iso(),
        "slides": slide_state,
    }
    write_state(state_path, state)
    return state


def reflow_existing_subtitles(
    project: Path,
    slides: list[SlideSpeech],
    *,
    measure_text: Callable[[str], float],
    target_width_px: int,
    hard_width_px: int,
    preferred_max_seconds: float,
) -> None:
    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        path = project / "subtitles" / f"{stem}.srt"
        original = parse_srt(path)
        if compact_text("".join(cue.text for cue in original)) != compact_text(slide.text):
            raise RuntimeError(f"reused subtitle differs from speech.md for {stem}")
        rewritten = reflow_existing_cues(
            original,
            measure_text=measure_text,
            target_width_px=target_width_px,
            hard_width_px=hard_width_px,
            preferred_max_seconds=preferred_max_seconds,
        )
        staged = temporary_path(path, ".srt")
        changed = True
        try:
            write_srt(rewritten, staged)
            changed = staged.read_bytes() != path.read_bytes()
            if changed:
                os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
        action = "updated" if changed else "unchanged"
        print(
            f"[Subtitle reflow] {stem}: {len(original)} -> {len(rewritten)} cues ({action})",
            flush=True,
        )


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "（").replace("}", "）")


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is required for slide geometry inspection") from exc
    with Image.open(path) as image:
        return image.size


def build_canvas_spec(project: Path, slides: list[SlideSpeech], deck_mode: str) -> CanvasSpec:
    dimensions = {
        image_dimensions(project / "origin_image" / f"slide_{slide.number:02d}.png")
        for slide in slides
    }
    if len(dimensions) != 1:
        raise ValueError(f"all rendered slides must have one common size, found {sorted(dimensions)}")
    source_width, source_height = next(iter(dimensions))
    if source_width < 1 or source_height < 1:
        raise ValueError("rendered slide dimensions must be positive")

    if deck_mode == NATIVE_DECK_MODE:
        source_ratio = source_width / source_height
        if abs(source_ratio - 16 / 9) > 0.005:
            raise ValueError(
                "native_generation requires 16:9 rendered slides; use existing_finished_ppt "
                "only after the existing-PPT direct-use gate is approved"
            )
        return CanvasSpec(
            deck_mode=deck_mode,
            source_width=source_width,
            source_height=source_height,
            slide_width=1920,
            slide_height=1080,
            video_width=1920,
            video_height=1080,
            band_y=990,
            band_height=90,
            font_size=44,
            target_width_px=1500,
            hard_width_px=1600,
            horizontal_margin_px=160,
            vertical_margin_px=18,
            outline_px=1.2,
            vertical_padding_px=20,
        )

    if deck_mode != EXISTING_PPT_DECK_MODE:
        raise ValueError(f"unsupported deck mode: {deck_mode!r}")
    if source_height < 540:
        raise ValueError(
            "existing finished PPT render height must be at least 540px; rerender the PPT "
            "at a clear video resolution without changing its aspect ratio"
        )
    video_width = source_width + source_width % 2
    band_height = max(1, round(source_height * 90 / 1080))
    if (source_height + band_height) % 2:
        band_height += 1
    hard_width = max(1, round(video_width * 1600 / 1920))
    target_width = min(hard_width, max(1, round(video_width * 1500 / 1920)))
    horizontal_margin = max(0, (video_width - hard_width) // 2)
    return CanvasSpec(
        deck_mode=deck_mode,
        source_width=source_width,
        source_height=source_height,
        slide_width=source_width,
        slide_height=source_height,
        video_width=video_width,
        video_height=source_height + band_height,
        band_y=source_height,
        band_height=band_height,
        font_size=max(1, round(source_height * 44 / 1080)),
        target_width_px=target_width,
        hard_width_px=hard_width,
        horizontal_margin_px=horizontal_margin,
        vertical_margin_px=max(1, round(source_height * 18 / 1080)),
        outline_px=max(0.5, round(source_height * 1.2 / 1080, 2)),
        vertical_padding_px=max(2, round(source_height * 20 / 1080)),
    )


def inspect_pptx_dynamic_content(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "transitions": [],
        "animations": [],
        "embedded_video": [],
        "embedded_audio": [],
    }
    video_suffixes = {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mpeg", ".mpg"}
    audio_suffixes = {".mp3", ".wav", ".m4a", ".aac", ".wma", ".aiff", ".aif"}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                xml = archive.read(name)
                if b"<p:transition" in xml or b":transition" in xml:
                    result["transitions"].append(name)
                if b"<p:timing" in xml or b":timing" in xml:
                    result["animations"].append(name)
            if name.startswith("ppt/media/"):
                suffix = Path(name).suffix.lower()
                if suffix in video_suffixes:
                    result["embedded_video"].append(name)
                elif suffix in audio_suffixes:
                    result["embedded_audio"].append(name)
    return result


def pptx_page_ratio(path: Path) -> float:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
    slide_size = next(
        (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "sldSz"),
        None,
    )
    if slide_size is None:
        raise ValueError("PPTX has no slide size declaration")
    width = int(slide_size.attrib["cx"])
    height = int(slide_size.attrib["cy"])
    if width <= 0 or height <= 0:
        raise ValueError("PPTX slide size must be positive")
    return width / height


def validate_existing_ppt_preflight(
    project: Path,
    manifest: dict[str, object],
    canvas: CanvasSpec,
) -> dict[str, object]:
    project = project.resolve()
    deck_input = manifest.get("deck_input")
    if not isinstance(deck_input, dict):
        raise ValueError("existing_finished_ppt mode requires manifest.deck_input")
    approvals = manifest.get("approvals")
    if not isinstance(approvals, dict) or approvals.get("existing_ppt_direct_use") != "approved":
        raise ValueError("existing finished PPT direct-use approval is not recorded")
    relative = str(deck_input.get("path") or "").strip()
    if not relative:
        raise ValueError("existing_finished_ppt mode requires deck_input.path")
    pptx = (project / relative).resolve()
    if pptx.suffix.lower() != ".pptx":
        raise ValueError("existing finished PPT must first be converted to a reviewable .pptx file")
    if not pptx.is_file():
        raise FileNotFoundError(f"existing finished PPT not found: {pptx}")
    recorded_hash = str(deck_input.get("sha256") or "")
    actual_hash = sha256(pptx)
    if recorded_hash != actual_hash:
        raise ValueError("existing finished PPT SHA-256 differs from manifest")
    expected_ratio = pptx_page_ratio(pptx)
    rendered_ratio = canvas.source_width / canvas.source_height
    if abs(rendered_ratio / expected_ratio - 1.0) > 0.005:
        raise ValueError(
            "rendered slide ratio differs from the approved PPTX page ratio; rerender without "
            "cropping, stretching or padding"
        )
    dynamic = inspect_pptx_dynamic_content(pptx)
    detected = any(dynamic.values())
    disposition = str(deck_input.get("dynamic_content_disposition") or "")
    if detected and disposition != "approved_static":
        raise ValueError(
            "PPTX contains animation, transition, embedded video or audio; pause until the user "
            "approves static conversion or selects a motion-preserving workflow"
        )
    if not detected and disposition not in {"none_detected", "approved_static"}:
        raise ValueError("record dynamic_content_disposition as none_detected")
    return {
        "path": str(pptx.relative_to(project)),
        "sha256": actual_hash,
        "pptx_page_ratio": round(expected_ratio, 8),
        "rendered_page_ratio": round(rendered_ratio, 8),
        "dynamic_content": dynamic,
        "dynamic_content_disposition": disposition,
    }


def write_ass(
    cues: list[SubtitleCue],
    path: Path,
    *,
    title: str,
    font: str,
    font_size: int,
    measure_text: Callable[[str], float],
    hard_width_px: int,
    play_res_x: int,
    play_res_y: int,
    horizontal_margin_px: int,
    vertical_margin_px: int,
    outline_px: float,
) -> int:
    longest = max(measure_text(cue.text) for cue in cues)
    if longest > hard_width_px:
        raise ValueError(f"display subtitle exceeds {hard_width_px}px: {longest:.1f}px")
    events = [
        f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,{ass_escape(cue.text)}"
        for cue in cues
    ]
    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline_px},0,2,{horizontal_margin_px},{horizontal_margin_px},{vertical_margin_px},1

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


def check_inputs(
    project: Path,
    slides: list[SlideSpeech],
    *,
    skip_tts: bool,
    audio_extension: str,
) -> None:
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
                f"audio/slide_{slide.number:02d}.{audio_extension}",
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
    audio_extension: str,
    audio_manifest: dict[str, object],
    font: str,
    font_file: Path,
    canvas: CanvasSpec,
    existing_ppt_preflight: dict[str, object] | None,
    measure_text: Callable[[str], float],
    preferred_max_seconds: float,
    tail_seconds: float,
    output_tag: str,
    resume: bool,
    subtitle_timing_source: str,
) -> dict[str, object]:
    subtitle_dir = project / "subtitles"
    video_dir = project / "video"
    work_dir = project / "qa" / f"media_work_{output_tag}_{canvas.deck_mode}"
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
        audio = project / "audio" / f"{stem}.{audio_extension}"
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
            font_size=canvas.font_size,
            measure_text=measure_text,
            hard_width_px=canvas.hard_width_px,
            play_res_x=canvas.video_width,
            play_res_y=canvas.video_height,
            horizontal_margin_px=canvas.horizontal_margin_px,
            vertical_margin_px=canvas.vertical_margin_px,
            outline_px=canvas.outline_px,
        )
        target_duration = media_duration(ffmpeg, audio) + tail_seconds
        newest_input = max(image.stat().st_mtime, audio.stat().st_mtime, srt.stat().st_mtime, ass.stat().st_mtime)
        can_reuse_clip = resume and clip.is_file() and clip.stat().st_size > 10_000 and clip.stat().st_mtime >= newest_input
        if not can_reuse_clip:
            ass_path = ass.as_posix().replace("'", r"\'")
            if canvas.deck_mode == NATIVE_DECK_MODE:
                picture_filter = "scale=1920:1080:force_original_aspect_ratio=disable"
            else:
                picture_filter = (
                    f"pad={canvas.video_width}:{canvas.video_height}:0:0:color=0x111827"
                )
            filter_graph = (
                f"{picture_filter},setsar=1,"
                f"drawbox=x=0:y={canvas.band_y}:w=iw:h={canvas.band_height}:"
                "color=0x111827:t=fill,"
                f"ass='{ass_path}':fontsdir='{fonts_dir}'"
            )
            pre_normalized = audio_manifest.get("pre_normalized") is True
            audio_filter = (
                f"apad=pad_dur={tail_seconds}"
                if pre_normalized
                else f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur={tail_seconds}"
            )
            if parse_srt(srt)[0].start > 2.0:
                shift = parse_srt(srt)[0].start - 0.45
                audio_filter = (
                    f"atrim=start={shift:.3f},asetpts=PTS-STARTPTS,"
                    + (
                        f"apad=pad_dur={tail_seconds}"
                        if pre_normalized
                        else f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur={tail_seconds}"
                    )
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
    layout_label = "字幕安全区" if canvas.deck_mode == NATIVE_DECK_MODE else "外置字幕栏"
    final_video = video_dir / f"{base}_{output_tag}_{layout_label}培训视频.mp4"
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
        "subtitle_timing_source": subtitle_timing_source,
        "subtitle_segmentation": "semantic sentence and clause boundaries with actual pixel-width QA",
        "deck_input_mode": canvas.deck_mode,
        "existing_ppt_preflight": existing_ppt_preflight,
        "display_cue_counts": display_counts,
        "slide_hashes": slide_hashes,
        "audio": audio_manifest,
        "video_spec": {
            "composer": "FFmpeg only",
            "ffmpeg_version": ffmpeg_version,
            "resolution": canvas.resolution,
            "width": canvas.video_width,
            "height": canvas.video_height,
            "fps": 30,
            "codec": "H.264 + AAC",
            "pixel_format": "yuv420p",
            "slide_mode": canvas.deck_mode,
            "slide_frame": {
                "source_width": canvas.source_width,
                "source_height": canvas.source_height,
                "rendered_width": canvas.slide_width,
                "rendered_height": canvas.slide_height,
                "comparison_height": canvas.comparison_height,
            },
            "subtitle_band": {
                "placement": "inside_slide" if canvas.deck_mode == NATIVE_DECK_MODE else "below_slide",
                "y": canvas.band_y,
                "height": canvas.band_height,
                "color": "#111827",
            },
            "subtitle_style": {
                "font": font,
                "font_file": str(font_file),
                "font_size_px": canvas.font_size,
                "outline_px": canvas.outline_px,
                "max_lines": 1,
                "target_width_px": canvas.target_width_px,
                "hard_width_px": canvas.hard_width_px,
                "preferred_max_seconds": preferred_max_seconds,
                "punctuation_policy": "paired and bound punctuation cannot be orphaned",
                "protected_tokens": "paired spans, policy identifiers, dates, amounts, percentages and units",
                "horizontal_margin_px": canvas.horizontal_margin_px,
                "vertical_margin_px": canvas.vertical_margin_px,
                "vertical_padding_px": canvas.vertical_padding_px,
            },
        },
    }
    manifest_path = project / "qa" / f"media_manifest_{output_tag}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument(
        "--provider",
        choices=("edge", "melo"),
        default=DEFAULT_TTS_PROVIDER,
        help="MeloTTS plus MFA is the approved local default; Edge requires explicit selection and current authorization",
    )
    parser.add_argument("--authorize-online-tts", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument(
        "--reflow-existing-subtitles",
        action="store_true",
        help="reuse approved audio/SRT timing and rewrite only semantic subtitle grouping",
    )
    parser.add_argument(
        "--reuse-provenance-tag",
        help="authorization/provenance state tag for reused audio; defaults to output tag",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--deck-mode", choices=DECK_MODES)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--rate", default="-5%")
    parser.add_argument("--list-local-voices", action="store_true")
    parser.add_argument("--preview-local-voice", action="store_true")
    parser.add_argument("--melo-python", type=Path)
    parser.add_argument("--melo-source", type=Path)
    parser.add_argument("--melo-speaker", default="ZH")
    parser.add_argument("--melo-speed", type=float, default=MELO_DEFAULT_SPEED)
    parser.add_argument("--confirm-local-voice")
    parser.add_argument(
        "--voice-confirmation-basis",
        default="",
        help="auditable reason for confirming the selected official local voice",
    )
    parser.add_argument("--mfa", type=Path)
    parser.add_argument("--mfa-root", type=Path)
    parser.add_argument("--pkuseg-home", type=Path)
    parser.add_argument("--local-cache-home", type=Path)
    parser.add_argument("--mfa-dictionary", default="mandarin_china_mfa")
    parser.add_argument("--mfa-acoustic-model", default="mandarin_mfa")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--subtitle-target-width-px", type=int)
    parser.add_argument("--subtitle-hard-width-px", type=int)
    parser.add_argument("--subtitle-preferred-max-seconds", type=float, default=7.0)
    parser.add_argument("--subtitle-font", default="Heiti SC")
    parser.add_argument(
        "--subtitle-font-file",
        type=Path,
        default=Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    )
    parser.add_argument("--subtitle-font-size", type=int)
    parser.add_argument("--subtitle-band-y", type=int)
    parser.add_argument("--subtitle-band-height", type=int)
    parser.add_argument("--tail-seconds", type=float, default=0.55)
    parser.add_argument("--output-tag", default=DEFAULT_OUTPUT_TAG)
    parser.add_argument("--tts-request-timeout", type=float, default=180.0)
    args = parser.parse_args()

    if (
        args.concurrency < 1
        or (args.subtitle_target_width_px is not None and args.subtitle_target_width_px < 1)
        or (args.subtitle_hard_width_px is not None and args.subtitle_hard_width_px < 1)
        or args.subtitle_preferred_max_seconds <= 0
        or args.tts_request_timeout <= 0
        or args.melo_speed <= 0
    ):
        parser.error("concurrency and subtitle geometry/timing limits must be positive")
    if (
        args.subtitle_hard_width_px is not None
        and args.subtitle_target_width_px is not None
        and args.subtitle_hard_width_px < args.subtitle_target_width_px
    ):
        parser.error("subtitle hard width cannot be lower than target width")
    if args.reflow_existing_subtitles and not args.skip_tts:
        parser.error("--reflow-existing-subtitles requires --skip-tts")
    if args.provider == "melo" and args.reflow_existing_subtitles:
        parser.error("Melo/MFA subtitles already use local actual-audio timing and cannot be reflowed")
    if (args.list_local_voices or args.preview_local_voice) and args.provider != "melo":
        parser.error("local voice listing and preview require --provider melo")
    output_tag = safe_filename(args.output_tag)
    if args.list_local_voices:
        print(
            json.dumps(
                {
                    "provider": "MeloTTS local",
                    "language": "ZH",
                    "official_fixed_voices": list(MELO_OFFICIAL_SPEAKERS),
                    "voice_cloning": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    project = args.project_dir.expanduser().resolve()
    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    policy_id = str(manifest.get("policy_id", "")).strip()
    title = str(manifest.get("policy_title", "")).strip()
    if not policy_id or not title:
        raise ValueError("manifest.json must contain policy_id and policy_title")
    deck_input = manifest.get("deck_input", {"mode": NATIVE_DECK_MODE})
    if not isinstance(deck_input, dict):
        raise ValueError("manifest.deck_input must be an object")
    recorded_deck_mode = str(deck_input.get("mode", NATIVE_DECK_MODE))
    if recorded_deck_mode not in DECK_MODES:
        raise ValueError(f"unsupported manifest deck mode: {recorded_deck_mode!r}")
    if args.deck_mode is not None and args.deck_mode != recorded_deck_mode:
        parser.error("--deck-mode must match the approved manifest.deck_input.mode")
    deck_mode = recorded_deck_mode
    slides = parse_speech(project / "speech.md")
    audio_extension = "wav" if args.provider == "melo" else "mp3"
    check_inputs(
        project,
        slides,
        skip_tts=args.skip_tts,
        audio_extension=audio_extension,
    )
    canvas = build_canvas_spec(project, slides, deck_mode)
    existing_ppt_preflight: dict[str, object] | None = None
    geometry_overrides = (
        args.subtitle_target_width_px,
        args.subtitle_hard_width_px,
        args.subtitle_font_size,
        args.subtitle_band_y,
        args.subtitle_band_height,
    )
    if deck_mode == EXISTING_PPT_DECK_MODE:
        if any(value is not None for value in geometry_overrides):
            parser.error(
                "existing_finished_ppt subtitle geometry is derived proportionally from the "
                "approved rendered PPT and cannot be overridden"
            )
        existing_ppt_preflight = validate_existing_ppt_preflight(project, manifest, canvas)
    else:
        target_width = args.subtitle_target_width_px or canvas.target_width_px
        hard_width = args.subtitle_hard_width_px or canvas.hard_width_px
        if hard_width < target_width:
            parser.error("subtitle hard width cannot be lower than target width")
        if hard_width > 1600:
            parser.error("native subtitle hard width cannot exceed 1600px")
        if args.subtitle_font_size not in {None, 44}:
            parser.error("native production subtitle font size is locked to 44px")
        if args.subtitle_band_y not in {None, 990} or args.subtitle_band_height not in {None, 90}:
            parser.error("native production subtitle band is locked to y=990 and height=90")
        canvas = replace(
            canvas,
            target_width_px=target_width,
            hard_width_px=hard_width,
        )
    ffmpeg = find_ffmpeg(args.ffmpeg)
    font_file = args.subtitle_font_file.expanduser().resolve()
    if not font_file.is_file():
        raise FileNotFoundError(f"subtitle font file not found: {font_file}")
    measure_text = make_text_measurer(font_file, canvas.font_size)

    # Keep the virtual-environment Python path itself; resolving its symlink would
    # bypass the environment and lose the installed Melo/Torch packages.
    melo_python = args.melo_python.expanduser().absolute() if args.melo_python else None
    melo_source = args.melo_source.expanduser().resolve() if args.melo_source else None
    mfa = args.mfa.expanduser().resolve() if args.mfa else None
    mfa_root = args.mfa_root.expanduser().resolve() if args.mfa_root else None
    pkuseg_home = args.pkuseg_home.expanduser().resolve() if args.pkuseg_home else None
    cache_home = (
        args.local_cache_home.expanduser().resolve()
        if args.local_cache_home
        else project / "qa" / "local_runtime_cache"
    )

    if args.provider == "melo":
        if melo_python is None or not melo_python.is_file():
            parser.error("--provider melo requires a valid --melo-python")
        if melo_source is not None and not melo_source.is_dir():
            parser.error("--melo-source must be a local MeloTTS source directory")
        if args.preview_local_voice:
            preview = generate_melo_preview(
                project,
                ffmpeg=ffmpeg,
                melo_python=melo_python,
                melo_source=melo_source,
                speaker=args.melo_speaker,
                speed=args.melo_speed,
                output_tag=output_tag,
            )
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return 0
        if mfa is None or not mfa.is_file() or mfa_root is None or pkuseg_home is None:
            parser.error(
                "--provider melo requires valid --mfa, --mfa-root and --pkuseg-home"
            )

    if args.check_only:
        if args.provider == "edge":
            provider_check = f"edge-tts={edge_version()}"
            notice = "NOTICE: full narration is sent only when --authorize-online-tts is present."
        else:
            assert mfa is not None
            provider_check = (
                f"MeloTTS=offline-local; speaker={args.melo_speaker}; "
                f"MFA={mfa_version(mfa, mfa_root)}"
            )
            blocking = sum(len(scan_blocking_terms(slide.text)) for slide in slides)
            notice = (
                f"NOTICE: local term gate currently finds {blocking} blocking Latin/number token(s); "
                "no online fallback is available."
            )
        print(
            f"PASS: locked pipeline {PIPELINE_VERSION}; {len(slides)} slides; "
            f"deck_mode={deck_mode}; canvas={canvas.resolution}; "
            f"{provider_check}; FFmpeg={ffmpeg}; font={font_file}"
        )
        if existing_ppt_preflight is not None:
            print(json.dumps(existing_ppt_preflight, ensure_ascii=False, indent=2))
        print(notice)
        return 0

    if args.provider == "edge":
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
                    measure_text=measure_text,
                    target_width_px=canvas.target_width_px,
                    hard_width_px=canvas.hard_width_px,
                    preferred_max_seconds=args.subtitle_preferred_max_seconds,
                    resume=args.resume,
                    output_tag=output_tag,
                    request_timeout_seconds=args.tts_request_timeout,
                )
            )
            online_authorized = True
            subtitle_timing_source = (
                "Edge WordBoundary events retained from the same stream as each MP3"
            )
        else:
            provenance_tag = safe_filename(args.reuse_provenance_tag or output_tag)
            state_path = project / "qa" / f"tts_state_{provenance_tag}.json"
            online_authorized = authorized_reuse_state(
                project,
                slides,
                state_path,
                voice=args.voice,
                rate=args.rate,
            )
            if not online_authorized:
                raise RuntimeError(
                    "reused Edge media has no matching authorization/provenance state; rerun TTS explicitly"
                )
            if args.reflow_existing_subtitles:
                reflow_existing_subtitles(
                    project,
                    slides,
                    measure_text=measure_text,
                    target_width_px=canvas.target_width_px,
                    hard_width_px=canvas.hard_width_px,
                    preferred_max_seconds=args.subtitle_preferred_max_seconds,
                )
                subtitle_timing_source = (
                    "semantic regrouping of reused approved SRT cue timing; approved MP3 files unchanged"
                )
            else:
                subtitle_timing_source = "reused approved SRT cue timing; approved MP3 files unchanged"
        audio_manifest: dict[str, object] = {
            "generator": "bundled Edge word-timed generator",
            "provider": "Microsoft Edge online TTS",
            "edge_tts_version": edge_version(),
            "voice": args.voice,
            "rate": normalize_rate(args.rate),
            "format": "MP3",
            "online_external_transfer_authorized": online_authorized,
            "sent_text_scope": "approved per-slide narration bodies from speech.md",
            "loudness_target_lufs": -16,
            "true_peak_target_dbfs": -1.5,
        }
    else:
        assert melo_python is not None
        assert mfa is not None
        assert mfa_root is not None
        assert pkuseg_home is not None
        if args.authorize_online_tts:
            parser.error("--authorize-online-tts is not used by the fail-closed local Melo provider")
        provenance_tag = output_tag
        if not args.skip_tts:
            if args.confirm_local_voice != args.melo_speaker:
                raise SystemExit(
                    "REFUSED: generate/listen to the official local voice first, then record the "
                    "selected speaker with --confirm-local-voice."
                )
            confirmation_basis = args.voice_confirmation_basis.strip()
            if not confirmation_basis:
                raise SystemExit(
                    "REFUSED: --voice-confirmation-basis is required for auditable local voice approval."
                )
            record_local_voice_selection(
                project,
                manifest,
                speaker=args.melo_speaker,
                speed=args.melo_speed,
                confirmation_basis=confirmation_basis,
                output_tag=output_tag,
            )
            local_state = generate_melo_mfa_media(
                project,
                slides,
                ffmpeg=ffmpeg,
                melo_python=melo_python,
                melo_source=melo_source,
                speaker=args.melo_speaker,
                speed=args.melo_speed,
                voice_confirmation_basis=confirmation_basis,
                mfa=mfa,
                mfa_root=mfa_root,
                pkuseg_home=pkuseg_home,
                cache_home=cache_home,
                mfa_dictionary=args.mfa_dictionary,
                mfa_acoustic_model=args.mfa_acoustic_model,
                measure_text=measure_text,
                target_width_px=canvas.target_width_px,
                hard_width_px=canvas.hard_width_px,
                preferred_max_seconds=args.subtitle_preferred_max_seconds,
                resume=args.resume,
                output_tag=output_tag,
            )
        else:
            provenance_tag = safe_filename(args.reuse_provenance_tag or output_tag)
            state_path = project / "qa" / f"tts_state_{provenance_tag}.json"
            local_state = local_reuse_state(
                project,
                slides,
                state_path,
                speaker=args.melo_speaker,
                speed=args.melo_speed,
                output_tag=provenance_tag,
            )
            if local_state is None:
                raise RuntimeError(
                    "reused Melo/MFA media has no matching local provenance state; rerun local TTS explicitly"
                )
        subtitle_timing_source = (
            "MFA actual-audio forced alignment from the same normalized WAV used in each clip"
        )
        audio_manifest = {
            "generator": "bundled fail-closed MeloTTS local generator",
            "provider": "MeloTTS local ZH",
            "speaker": args.melo_speaker,
            "speed": args.melo_speed,
            "format": "WAV PCM 16-bit mono 48 kHz",
            "pre_normalized": True,
            "official_fixed_voice": True,
            "voice_confirmed": local_state["voice_confirmed"],
            "voice_confirmation_basis": local_state["voice_confirmation_basis"],
            "voice_cloning": False,
            "local_only": True,
            "online_external_transfer_authorized": False,
            "network_fallback": False,
            "model_loading_policy": local_state["model_loading_policy"],
            "term_gate": local_state["term_gate"],
            "provenance_tag": provenance_tag,
            "alignment_method": local_state["alignment_method"],
            "mfa_version": local_state["mfa_version"],
            "mfa_dictionary": local_state["mfa_dictionary"],
            "mfa_acoustic_model": local_state["mfa_acoustic_model"],
            "loudness_target_lufs": MELO_LOUDNESS["I"],
            "true_peak_target_dbfs": MELO_LOUDNESS["TP"],
        }

    output = compose_video(
        project,
        slides,
        ffmpeg=ffmpeg,
        policy_id=policy_id,
        title=title,
        audio_extension=audio_extension,
        audio_manifest=audio_manifest,
        font=args.subtitle_font,
        font_file=font_file,
        canvas=canvas,
        existing_ppt_preflight=existing_ppt_preflight,
        measure_text=measure_text,
        preferred_max_seconds=args.subtitle_preferred_max_seconds,
        tail_seconds=args.tail_seconds,
        output_tag=output_tag,
        resume=args.resume,
        subtitle_timing_source=subtitle_timing_source,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
