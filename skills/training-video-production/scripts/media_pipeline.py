#!/usr/bin/env python3
"""Generate per-slide Edge TTS and compose a subtitle-safe policy video with FFmpeg."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


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


def find_notes_to_audio(project: Path, explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())
    relative = Path("third_party/ppt-master/skills/ppt-master/scripts/notes_to_audio.py")
    candidates.extend([Path.cwd() / relative, project / relative, project.parent / relative])
    env_path = os.environ.get("PPT_MASTER_NOTES_TO_AUDIO")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(
        "PPT Master notes_to_audio.py not found; pass --notes-to-audio or set "
        "PPT_MASTER_NOTES_TO_AUDIO"
    )


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
        pass
    raise FileNotFoundError("FFmpeg not found; pass --ffmpeg or install ffmpeg/imageio-ffmpeg")


def prepare_ppt_master_project(project: Path, slides: list[SlideSpeech]) -> Path:
    ppt_project = project / "ppt_master_project"
    notes_dir = ppt_project / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for slide in slides:
        (notes_dir / f"slide_{slide.number:02d}.md").write_text(
            f"# Slide {slide.number}: {slide.title}\n\n{slide.text}\n",
            encoding="utf-8",
        )
    return ppt_project


def generate_edge_audio(
    project: Path,
    ppt_project: Path,
    slides: list[SlideSpeech],
    *,
    notes_to_audio: Path,
    tts_python: Path,
    voice: str,
    rate: str,
    concurrency: int,
    source_max_chars: int,
) -> None:
    audio_dir = project / "audio"
    subtitle_dir = project / "subtitles"
    audio_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(tts_python),
            str(notes_to_audio),
            str(ppt_project),
            "--provider",
            "edge",
            "--voice",
            voice,
            f"--rate={rate}",
            "--concurrency",
            str(concurrency),
            "--subtitle-max-chars",
            str(source_max_chars),
            "--output",
            str(audio_dir),
        ]
    )
    source_subtitles = ppt_project / "notes/subtitles"
    for slide in slides:
        source = source_subtitles / f"slide_{slide.number:02d}.srt"
        if not source.is_file():
            raise FileNotFoundError(f"missing PPT Master subtitle: {source}")
        shutil.copy2(source, subtitle_dir / source.name)


def srt_seconds(value: str) -> float:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt(path: Path) -> list[SubtitleCue]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    blocks = re.split(r"\r?\n\s*\r?\n", raw)
    pattern = re.compile(
        r"^(?:\d+\s*\n)?(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})\s*\n(?P<text>[\s\S]+)$"
    )
    cues: list[SubtitleCue] = []
    for block in blocks:
        match = pattern.match(block.strip())
        if not match:
            raise ValueError(f"malformed SRT block in {path}: {block[:80]!r}")
        text = "".join(part.strip() for part in match.group("text").splitlines())
        cue = SubtitleCue(srt_seconds(match.group("start")), srt_seconds(match.group("end")), text)
        if cue.end <= cue.start or (cues and cue.start < cues[-1].end):
            raise ValueError(f"invalid or overlapping subtitle cue in {path}")
        cues.append(cue)
    if not cues:
        raise ValueError(f"no subtitle cues in {path}")
    return cues


def merge_display_cues(cues: list[SubtitleCue], max_chars: int) -> list[SubtitleCue]:
    merged: list[SubtitleCue] = []
    punctuation = ("。", "！", "？", "；", "：", "，", ".", "!", "?", ";", ":", ",")
    for cue in cues:
        if (
            merged
            and not merged[-1].text.endswith(punctuation)
            and len(merged[-1].text + cue.text) <= max_chars
        ):
            previous = merged[-1]
            merged[-1] = SubtitleCue(previous.start, cue.end, previous.text + cue.text)
        else:
            merged.append(cue)
    longest = max(len(cue.text) for cue in merged)
    if longest > max_chars:
        raise ValueError(f"display subtitle exceeds {max_chars} characters: {longest}")
    return merged


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def write_ass(
    cues: list[SubtitleCue],
    path: Path,
    *,
    title: str,
    font: str,
    font_size: int,
    display_max_chars: int,
) -> int:
    display_cues = merge_display_cues(cues, display_max_chars)
    events: list[str] = []
    for cue in display_cues:
        text = cue.text.replace("{", "（").replace("}", "）")
        events.append(
            f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,{text}"
        )
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
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return len(display_cues)


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


def write_global_srt(
    slide_cues: list[list[SubtitleCue]], slide_durations: list[float], output: Path
) -> int:
    lines: list[str] = []
    cue_number = 1
    offset = 0.0
    for cues, duration in zip(slide_cues, slide_durations):
        for cue in cues:
            lines.extend(
                [
                    str(cue_number),
                    f"{srt_time(offset + cue.start)} --> {srt_time(offset + cue.end)}",
                    cue.text,
                    "",
                ]
            )
            cue_number += 1
        offset += duration
    output.write_text("\n".join(lines), encoding="utf-8")
    return cue_number - 1


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", value).strip(" ._")
    return cleaned or "policy"


def check_inputs(project: Path, slides: list[SlideSpeech], skip_tts: bool) -> None:
    missing_images = [
        str(project / "origin_image" / f"slide_{slide.number:02d}.png")
        for slide in slides
        if not (project / "origin_image" / f"slide_{slide.number:02d}.png").is_file()
    ]
    if missing_images:
        raise FileNotFoundError("missing slide images:\n" + "\n".join(missing_images))
    if skip_tts:
        for slide in slides:
            for relative in (f"audio/slide_{slide.number:02d}.mp3", f"subtitles/slide_{slide.number:02d}.srt"):
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
    font_size: int,
    band_y: int,
    band_height: int,
    source_max_chars: int,
    display_max_chars: int,
    tail_seconds: float,
    online_authorized: bool,
) -> dict[str, object]:
    subtitle_dir = project / "subtitles"
    video_dir = project / "video"
    work_dir = project / "qa/media_work"
    clip_dir = work_dir / "clips"
    ass_dir = work_dir / "ass"
    for directory in (video_dir, clip_dir, ass_dir):
        directory.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    all_cues: list[list[SubtitleCue]] = []
    durations: list[float] = []
    display_counts: dict[str, int] = {}

    for slide in slides:
        stem = f"slide_{slide.number:02d}"
        image = project / "origin_image" / f"{stem}.png"
        audio = project / "audio" / f"{stem}.mp3"
        srt = subtitle_dir / f"{stem}.srt"
        ass = ass_dir / f"{stem}.ass"
        clip = clip_dir / f"{stem}.mp4"
        cues = parse_srt(srt)
        display_counts[stem] = write_ass(
            cues,
            ass,
            title=f"{policy_id} {stem}",
            font=font,
            font_size=font_size,
            display_max_chars=display_max_chars,
        )
        target_duration = media_duration(ffmpeg, audio) + tail_seconds
        ass_path = ass.as_posix().replace("'", r"\'")
        filter_graph = (
            "scale=1920:1080:force_original_aspect_ratio=disable,"
            f"drawbox=x=0:y={band_y}:w=iw:h={band_height}:color=0x111827:t=fill,"
            f"ass='{ass_path}':fontsdir='/System/Library/Fonts'"
        )
        run(
            [
                str(ffmpeg), "-y", "-loop", "1", "-framerate", "30", "-i", str(image),
                "-i", str(audio), "-vf", filter_graph, "-map", "0:v:0", "-map", "1:a:0",
                "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=pad_dur={tail_seconds}",
                "-t", f"{target_duration:.3f}", "-c:v", "libx264", "-preset", "medium",
                "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac",
                "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                str(clip),
            ]
        )
        clips.append(clip)
        all_cues.append(cues)
        durations.append(media_duration(ffmpeg, clip))
        print(f"[Video] {stem}: {durations[-1]:.2f}s", flush=True)

    concat_list = clip_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    base = safe_filename(f"{policy_id}_{title}")
    final_video = video_dir / f"{base}_字幕安全区培训视频.mp4"
    run(
        [
            str(ffmpeg), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart", str(final_video),
        ]
    )
    global_srt = subtitle_dir / f"{base}_全程字幕.srt"
    cue_count = write_global_srt(all_cues, durations, global_srt)
    manifest: dict[str, object] = {
        "policy_id": policy_id,
        "policy_title": title,
        "source_of_truth": "speech.md",
        "video": str(final_video.relative_to(project)),
        "global_subtitles": str(global_srt.relative_to(project)),
        "slide_count": len(slides),
        "slide_durations_seconds": {
            f"slide_{slide.number:02d}": round(duration, 3)
            for slide, duration in zip(slides, durations)
        },
        "duration_seconds": round(media_duration(ffmpeg, final_video), 3),
        "source_subtitle_cues": cue_count,
        "display_cue_counts": display_counts,
        "audio": {
            "generator": "PPT Master notes_to_audio.py or approved reused files",
            "provider": "edge" if online_authorized else "reused_or_local",
            "voice": voice if online_authorized else None,
            "rate": rate if online_authorized else None,
            "online_external_transfer_authorized": online_authorized,
            "loudness_target_lufs": -16,
        },
        "video_spec": {
            "resolution": "1920x1080",
            "fps": 30,
            "codec": "H.264 + AAC",
            "slide_mode": "full-bleed 16:9",
            "subtitle_band": {"y": band_y, "height": band_height},
            "subtitle_style": {
                "font": font,
                "font_size_px": font_size,
                "max_lines": 1,
                "source_max_chars": source_max_chars,
                "max_display_chars": display_max_chars,
            },
        },
    }
    qa_dir = project / "qa"
    qa_dir.mkdir(exist_ok=True)
    (qa_dir / "media_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--provider", choices=("edge",), default="edge")
    parser.add_argument("--authorize-online-tts", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--notes-to-audio", type=Path)
    parser.add_argument("--tts-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--rate", default="-5%")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--subtitle-source-max-chars", type=int, default=20)
    parser.add_argument("--subtitle-display-max-chars", type=int, default=24)
    parser.add_argument("--subtitle-font", default="Heiti SC")
    parser.add_argument("--subtitle-font-size", type=int, default=44)
    parser.add_argument("--subtitle-band-y", type=int, default=990)
    parser.add_argument("--subtitle-band-height", type=int, default=90)
    parser.add_argument("--tail-seconds", type=float, default=0.55)
    args = parser.parse_args()

    project = args.project_dir.expanduser().resolve()
    manifest_path = project / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy_id = str(manifest.get("policy_id", "")).strip()
    title = str(manifest.get("policy_title", "")).strip()
    if not policy_id or not title:
        raise ValueError("manifest.json must contain policy_id and policy_title")
    slides = parse_speech(project / "speech.md")
    check_inputs(project, slides, args.skip_tts)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    notes_to_audio: Path | None = None
    if not args.skip_tts:
        notes_to_audio = find_notes_to_audio(project, args.notes_to_audio)

    if args.check_only:
        print(f"PASS: {len(slides)} slides; FFmpeg={ffmpeg}")
        if notes_to_audio:
            print(f"PPT Master={notes_to_audio}")
        if not args.skip_tts:
            print("NOTICE: explicit current-material authorization is still required before online TTS.")
        return 0

    if not args.skip_tts and not args.authorize_online_tts:
        raise SystemExit(
            "REFUSED: Edge TTS sends narration to Microsoft online services. Obtain explicit "
            "authorization for the current material, then add --authorize-online-tts."
        )

    if not args.skip_tts:
        ppt_project = prepare_ppt_master_project(project, slides)
        assert notes_to_audio is not None
        generate_edge_audio(
            project,
            ppt_project,
            slides,
            notes_to_audio=notes_to_audio,
            tts_python=args.tts_python.expanduser().resolve(),
            voice=args.voice,
            rate=args.rate,
            concurrency=args.concurrency,
            source_max_chars=args.subtitle_source_max_chars,
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
        font_size=args.subtitle_font_size,
        band_y=args.subtitle_band_y,
        band_height=args.subtitle_band_height,
        source_max_chars=args.subtitle_source_max_chars,
        display_max_chars=args.subtitle_display_max_chars,
        tail_seconds=args.tail_seconds,
        online_authorized=not args.skip_tts and args.authorize_online_tts,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
