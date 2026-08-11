#!/usr/bin/env python3
"""Mechanical QA for a subtitle-safe policy training video."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def find_ffmpeg(explicit: Path | None) -> Path:
    if explicit:
        path = explicit.expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"FFmpeg not found: {path}")
    system = shutil.which("ffmpeg")
    if system:
        return Path(system).resolve()
    try:
        import imageio_ffmpeg  # type: ignore

        return Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    except (ImportError, RuntimeError):
        raise FileNotFoundError("FFmpeg not found; pass --ffmpeg") from None


def parse_narration(path: Path) -> tuple[int, str]:
    source = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^## Slide\s+\d+:.*$", source, re.MULTILINE))
    bodies: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        block = source[heading.end():end].split("\n---", 1)[0]
        bodies.append("".join(line.strip() for line in block.splitlines() if line.strip()))
    return len(headings), "".join(bodies)


def parse_srt_text(path: Path) -> tuple[int, str, int]:
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
    texts: list[str] = []
    longest = 0
    for block in blocks:
        lines = block.splitlines()
        time_index = next((index for index, line in enumerate(lines) if " --> " in line), None)
        if time_index is None or time_index + 1 >= len(lines):
            raise ValueError(f"malformed SRT block in {path}: {block[:80]!r}")
        text = "".join(lines[time_index + 1:]).strip()
        texts.append(text)
        longest = max(longest, len(text))
    return len(texts), "".join(texts), longest


def read_ass_events(path: Path) -> list[str]:
    return [
        line.split(",", 9)[9]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:") and len(line.split(",", 9)) == 10
    ]


def duration_seconds(probe: str) -> float | None:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", probe)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def write_report(path: Path, result: dict[str, object]) -> None:
    checks = result["checks"]
    assert isinstance(checks, dict)
    lines = [
        "# 媒体技术检查报告",
        "",
        f"- 结论：**{str(result['status']).upper()}**",
        f"- 检查模式：{result['mode']}",
        f"- 视频时长：{result.get('video_duration_seconds')} 秒",
        f"- 字幕字符：{result.get('subtitle_characters')}",
        "",
        "## 检查项",
        "",
    ]
    lines.extend(
        f"- {'通过' if passed else '未通过'}：`{name}`"
        for name, passed in checks.items()
    )
    if result.get("full_checks_skipped"):
        lines.extend(
            [
                "",
                "> 当前为快速结构检查，未执行完整解码、响度、静音和逐页画面对照；正式交付必须执行完整检查。",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--fast", action="store_true", help="skip decode, loudness, silence and frame QA")
    args = parser.parse_args()

    project = args.project_dir.expanduser().resolve()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    media_manifest = json.loads((project / "qa/media_manifest.json").read_text(encoding="utf-8"))
    video = project / str(media_manifest["video"])
    global_srt = project / str(media_manifest["global_subtitles"])
    slide_count = int(media_manifest["slide_count"])
    font = str(media_manifest["video_spec"]["subtitle_style"]["font"])
    font_size = int(media_manifest["video_spec"]["subtitle_style"]["font_size_px"])
    source_limit = int(media_manifest["video_spec"]["subtitle_style"].get("source_max_chars", 20))
    display_limit = int(media_manifest["video_spec"]["subtitle_style"]["max_display_chars"])
    band_y = int(media_manifest["video_spec"]["subtitle_band"]["y"])

    probe = run([str(ffmpeg), "-hide_banner", "-i", str(video)]).stderr
    speech_count, expected_text = parse_narration(project / "speech.md")
    cue_count, subtitle_text, max_source_length = parse_srt_text(global_srt)
    audio_files = sorted((project / "audio").glob("slide_*.mp3"))
    subtitle_files = sorted((project / "subtitles").glob("slide_*.srt"))
    ass_files = sorted((project / "qa/media_work/ass").glob("slide_*.ass"))

    display_text_exact = True
    display_max_length = 0
    for ass_path in ass_files:
        events = read_ass_events(ass_path)
        if events:
            display_max_length = max(display_max_length, *(len(text) for text in events))
        number = int(ass_path.stem.split("_")[-1])
        _, source_text, _ = parse_srt_text(project / "subtitles" / f"slide_{number:02d}.srt")
        display_text_exact = display_text_exact and "".join(events) == source_text

    checks: dict[str, bool] = {
        "video_file_present": video.is_file() and video.stat().st_size > 0,
        "speech_slide_count_matches": speech_count == slide_count,
        "audio_count_matches": len(audio_files) == slide_count and all(p.stat().st_size > 10_000 for p in audio_files),
        "per_slide_subtitle_count_matches": len(subtitle_files) == slide_count,
        "ass_count_matches": len(ass_files) == slide_count,
        "subtitle_text_exact": expected_text == subtitle_text,
        "source_subtitle_cues_within_limit": max_source_length <= source_limit,
        "display_subtitles_exact_and_within_limit": display_text_exact and display_max_length <= display_limit,
        "ass_uses_approved_font_size": all(
            f"Style: Caption,{font},{font_size}," in path.read_text(encoding="utf-8")
            for path in ass_files
        ),
        "video_is_1920x1080": "1920x1080" in probe,
        "video_is_30fps": "30 fps" in probe,
        "video_codec_h264": "Video: h264" in probe,
        "audio_codec_aac_48khz": "Audio: aac" in probe and "48000 Hz" in probe,
    }

    frame_differences: dict[str, float] = {}
    integrated_loudness: float | None = None
    true_peak: float | None = None
    silence_events: int | None = None
    if not args.fast:
        try:
            from PIL import Image, ImageChops, ImageStat
        except ImportError:
            raise SystemExit("Pillow is required for full frame QA; install Pillow or use --fast") from None

        decode = run([str(ffmpeg), "-v", "error", "-i", str(video), "-f", "null", "-"])
        silence = run(
            [
                str(ffmpeg), "-hide_banner", "-i", str(video), "-vn", "-af",
                "silencedetect=noise=-45dB:d=2.0", "-f", "null", "-",
            ]
        ).stderr
        loudness = run(
            [
                str(ffmpeg), "-hide_banner", "-i", str(video), "-vn", "-af",
                "ebur128=peak=true", "-f", "null", "-",
            ]
        ).stderr
        silence_events = len(re.findall(r"silence_start:", silence))
        integrated = re.findall(r"I:\s*(-?\d+\.\d+) LUFS", loudness)
        peaks = re.findall(r"Peak:\s*(-?\d+\.\d+) dBFS", loudness)
        integrated_loudness = float(integrated[-1]) if integrated else None
        true_peak = float(peaks[-1]) if peaks else None

        starts: list[float] = []
        cursor = 0.0
        durations = media_manifest["slide_durations_seconds"]
        for number in range(1, slide_count + 1):
            starts.append(cursor)
            cursor += float(durations[f"slide_{number:02d}"])
        frame_dir = project / "qa/media_frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        for number, start in enumerate(starts, 1):
            frame = frame_dir / f"slide_{number:02d}_lead.png"
            extraction = run(
                [
                    str(ffmpeg), "-y", "-ss", f"{start + 0.20:.3f}", "-i", str(video),
                    "-frames:v", "1", "-update", "1", str(frame),
                ]
            )
            if extraction.returncode != 0:
                raise RuntimeError(f"frame extraction failed for slide {number}")
            expected = Image.open(project / "origin_image" / f"slide_{number:02d}.png").convert("RGB")
            expected = expected.resize((1920, 1080)).crop((0, 0, 1920, band_y)).resize((320, 165))
            actual = Image.open(frame).convert("RGB").crop((0, 0, 1920, band_y)).resize((320, 165))
            means = ImageStat.Stat(ImageChops.difference(expected, actual)).mean
            frame_differences[f"slide_{number:02d}"] = round(sum(means) / 3, 4)

        checks.update(
            {
                "video_decodes_without_error": decode.returncode == 0 and not decode.stderr.strip(),
                "no_silence_over_2_seconds": silence_events == 0,
                "slide_order_and_top_content_match": bool(frame_differences)
                and max(frame_differences.values()) < 3.0,
            }
        )

    status = "pass" if all(checks.values()) else "fail"
    result: dict[str, object] = {
        "status": status,
        "mode": "fast_structure" if args.fast else "full",
        "full_checks_skipped": args.fast,
        "checks": checks,
        "video_duration_seconds": duration_seconds(probe),
        "video_size_bytes": video.stat().st_size if video.is_file() else None,
        "integrated_loudness_lufs": integrated_loudness,
        "true_peak_dbfs": true_peak,
        "silence_events_over_2_seconds": silence_events,
        "subtitle_cues": cue_count,
        "subtitle_characters": len(subtitle_text),
        "max_source_cue_chars": max_source_length,
        "max_display_cue_chars": display_max_length,
        "frame_mean_absolute_differences_top_safe_area": frame_differences,
    }
    qa_dir = project / "qa"
    qa_dir.mkdir(exist_ok=True)
    (qa_dir / "media_qa.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_report(qa_dir / "媒体技术检查报告.md", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
