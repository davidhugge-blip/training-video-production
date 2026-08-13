#!/usr/bin/env python3
"""Full mechanical QA for the locked policy-training media pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", value).strip(" ._")
    return cleaned or "policy"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


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


def parse_narrations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^## Slide\s+\d+:.*$", source, re.MULTILINE))
    bodies: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        block = source[heading.end():end].split("\n---", 1)[0]
        bodies.append("\n".join(line.strip() for line in block.splitlines() if line.strip()))
    return bodies


def parse_srt(path: Path) -> tuple[list[tuple[float, float, str]], int]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    blocks = re.split(r"\r?\n\s*\r?\n", raw)
    cues: list[tuple[float, float, str]] = []
    longest = 0
    for block in blocks:
        lines = block.splitlines()
        time_index = next((index for index, line in enumerate(lines) if " --> " in line), None)
        if time_index is None or time_index + 1 >= len(lines):
            raise ValueError(f"malformed SRT block in {path}: {block[:80]!r}")
        start_raw, end_raw = lines[time_index].split(" --> ")
        text = "".join(lines[time_index + 1:]).strip()
        start = timestamp_seconds(start_raw)
        end = timestamp_seconds(end_raw)
        if end <= start or (cues and start < cues[-1][1]):
            raise ValueError(f"invalid or overlapping SRT cue in {path}")
        cues.append((start, end, text))
        longest = max(longest, len(compact_text(text)))
    if not cues:
        raise ValueError(f"no subtitle cues in {path}")
    return cues, longest


def timestamp_seconds(value: str) -> float:
    hours, minutes, remainder = value.strip().split(":")
    seconds, milliseconds = remainder.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


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


def measure_subtitles(
    subtitle_files: list[Path],
    *,
    font_file: Path,
    font_size: int,
    available_width: int,
    band_height: int,
) -> tuple[bool, float, int, str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise SystemExit("Pillow is required for subtitle geometry QA") from exc
    font = ImageFont.truetype(str(font_file), font_size)
    canvas = Image.new("RGB", (1920, 1080), "black")
    draw = ImageDraw.Draw(canvas)
    maximum_width = 0.0
    maximum_height = 0
    widest_text = ""
    for subtitle in subtitle_files:
        cues, _ = parse_srt(subtitle)
        for _, _, text in cues:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=2)
            width = right - left
            height = bottom - top
            if width > maximum_width:
                maximum_width = width
                widest_text = text
            maximum_height = max(maximum_height, height)
    return (
        maximum_width <= available_width and maximum_height <= band_height - 20,
        maximum_width,
        maximum_height,
        widest_text,
    )


def write_report(path: Path, result: dict[str, object]) -> None:
    checks = result["checks"]
    assert isinstance(checks, dict)
    lines = [
        "# 媒体技术检查报告",
        "",
        f"- 结论：**{str(result['status']).upper()}**",
        f"- 技能媒体链路版本：{result.get('pipeline_version')}",
        f"- 检查模式：{result['mode']}",
        f"- 视频时长：{result.get('video_duration_seconds')} 秒",
        f"- 灯片数量：{result.get('slide_count')}",
        f"- 字幕提示数量：{result.get('subtitle_cues')}",
        f"- 最大字幕实际宽度：{result.get('max_subtitle_width_px')} px",
        f"- 综合响度：{result.get('integrated_loudness_lufs')} LUFS",
        f"- 真峰值：{result.get('true_peak_dbfs')} dBFS",
        "",
        "## 检查项",
        "",
    ]
    lines.extend(f"- {'通过' if passed else '未通过'}：`{name}`" for name, passed in checks.items())
    if result.get("full_checks_skipped"):
        lines.extend(
            [
                "",
                "> 当前为快速结构检查；正式交付必须执行完整解码、响度、静音和75页画面对照。",
            ]
        )
    lines.extend(
        [
            "",
            "## 人工门禁",
            "",
            "- 技术通过不等于正式发布；仍需制度负责人核验内容，并由基层员工代表试看。",
            "- 人工试看重点：术语读音、自然度、停顿、字幕舒适度和规则可理解性。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--output-tag", default="V2")
    parser.add_argument("--fast", action="store_true", help="skip decode, loudness, silence and frame QA")
    args = parser.parse_args()

    project = args.project_dir.expanduser().resolve()
    output_tag = safe_filename(args.output_tag)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    manifest_path = project / "qa" / f"media_manifest_{output_tag}.json"
    media_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    video = project / str(media_manifest["video"])
    global_srt = project / str(media_manifest["global_subtitles"])
    slide_count = int(media_manifest["slide_count"])
    subtitle_style = media_manifest["video_spec"]["subtitle_style"]
    font = str(subtitle_style["font"])
    font_file = Path(str(subtitle_style["font_file"]))
    font_size = int(subtitle_style["font_size_px"])
    source_limit = int(subtitle_style["source_max_chars"])
    display_limit = int(subtitle_style["max_display_chars"])
    horizontal_margin = int(subtitle_style["horizontal_margin_px"])
    band_y = int(media_manifest["video_spec"]["subtitle_band"]["y"])
    band_height = int(media_manifest["video_spec"]["subtitle_band"]["height"])
    ass_dir = project / str(media_manifest["ass_dir"])
    clip_dir = project / str(media_manifest["clip_dir"])

    probe = run([str(ffmpeg), "-hide_banner", "-i", str(video)]).stderr
    narrations = parse_narrations(project / "speech.md")
    audio_files = [project / "audio" / f"slide_{number:02d}.mp3" for number in range(1, slide_count + 1)]
    subtitle_files = [project / "subtitles" / f"slide_{number:02d}.srt" for number in range(1, slide_count + 1)]
    ass_files = [ass_dir / f"slide_{number:02d}.ass" for number in range(1, slide_count + 1)]
    clip_files = [clip_dir / f"slide_{number:02d}.mp4" for number in range(1, slide_count + 1)]

    subtitle_cues = 0
    longest_source = 0
    per_slide_text_exact = True
    per_slide_timing_valid = True
    cue_coverage_ratios: list[float] = []
    for number, (narration, subtitle) in enumerate(zip(narrations, subtitle_files), 1):
        cues, longest = parse_srt(subtitle)
        subtitle_cues += len(cues)
        longest_source = max(longest_source, longest)
        per_slide_text_exact = per_slide_text_exact and compact_text("".join(cue[2] for cue in cues)) == compact_text(narration)
        audio_duration = float(media_manifest["slide_durations_seconds"][f"slide_{number:02d}"]) - 0.55
        per_slide_timing_valid = per_slide_timing_valid and cues[-1][1] <= audio_duration + 0.10
        cue_coverage_ratios.append(cues[-1][1] / audio_duration if audio_duration > 0 else 0)

    global_cues, _ = parse_srt(global_srt)
    global_text = compact_text("".join(cue[2] for cue in global_cues))
    expected_text = compact_text("".join(narrations))
    display_text_exact = True
    display_max_length = 0
    for number, ass_path in enumerate(ass_files, 1):
        events = read_ass_events(ass_path)
        display_max_length = max(display_max_length, *(len(compact_text(text)) for text in events))
        cues, _ = parse_srt(subtitle_files[number - 1])
        display_text_exact = display_text_exact and compact_text("".join(events)) == compact_text("".join(cue[2] for cue in cues))

    geometry_ok, maximum_width, maximum_height, widest_text = measure_subtitles(
        subtitle_files,
        font_file=font_file,
        font_size=font_size,
        available_width=1920 - 2 * horizontal_margin,
        band_height=band_height,
    )
    hashes = media_manifest["slide_hashes"]
    hashes_exact = True
    for number in range(1, slide_count + 1):
        stem = f"slide_{number:02d}"
        entry = hashes[stem]
        for key, path in (
            ("image_sha256", project / "origin_image" / f"{stem}.png"),
            ("audio_sha256", audio_files[number - 1]),
            ("subtitle_sha256", subtitle_files[number - 1]),
            ("ass_sha256", ass_files[number - 1]),
            ("clip_sha256", clip_files[number - 1]),
        ):
            hashes_exact = hashes_exact and path.is_file() and sha256(path) == entry[key]

    checks: dict[str, bool] = {
        "locked_pipeline_version_2": media_manifest.get("pipeline_version") == "2.0",
        "no_implicit_tool_fallback": media_manifest.get("fallback_policy") == "fail_closed_no_tool_switch",
        "online_tts_authorization_recorded": media_manifest["audio"].get("online_external_transfer_authorized") is True,
        "video_file_present": video.is_file() and video.stat().st_size > 0,
        "speech_slide_count_matches": len(narrations) == slide_count,
        "audio_count_and_format_match": len(audio_files) == slide_count and all(path.is_file() and path.stat().st_size > 10_000 for path in audio_files),
        "per_slide_subtitle_count_matches": len(subtitle_files) == slide_count and all(path.is_file() for path in subtitle_files),
        "ass_count_matches": len(ass_files) == slide_count and all(path.is_file() for path in ass_files),
        "clip_count_matches": len(clip_files) == slide_count and all(path.is_file() for path in clip_files),
        "per_slide_subtitle_text_exact": per_slide_text_exact,
        "global_subtitle_text_exact": expected_text == global_text,
        "word_timing_within_audio": per_slide_timing_valid and min(cue_coverage_ratios) >= 0.75,
        "source_subtitle_cues_within_limit": longest_source <= source_limit,
        "display_subtitles_exact_and_within_limit": display_text_exact and display_max_length <= display_limit,
        "subtitle_geometry_fits_single_line_band": geometry_ok,
        "ass_uses_approved_font_size": all(
            f"Style: Caption,{font},{font_size}," in path.read_text(encoding="utf-8") for path in ass_files
        ),
        "artifact_hashes_match_manifest": hashes_exact,
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
        except ImportError as exc:
            raise SystemExit("Pillow is required for full frame QA") from exc

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
        frame_dir = project / "qa" / f"media_frames_{output_tag}"
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
                "integrated_loudness_near_minus_16_lufs": integrated_loudness is not None and -17.5 <= integrated_loudness <= -14.5,
                "true_peak_below_minus_1_dbfs": true_peak is not None and true_peak <= -1.0,
                "all_slide_order_and_top_content_match": bool(frame_differences) and max(frame_differences.values()) < 3.0,
            }
        )

    status = "pass" if all(checks.values()) else "fail"
    result: dict[str, object] = {
        "status": status,
        "pipeline_version": media_manifest.get("pipeline_version"),
        "mode": "fast_structure" if args.fast else "full",
        "full_checks_skipped": args.fast,
        "checks": checks,
        "video": str(video.relative_to(project)),
        "video_duration_seconds": duration_seconds(probe),
        "video_size_bytes": video.stat().st_size if video.is_file() else None,
        "slide_count": slide_count,
        "integrated_loudness_lufs": integrated_loudness,
        "true_peak_dbfs": true_peak,
        "silence_events_over_2_seconds": silence_events,
        "subtitle_cues": subtitle_cues,
        "max_source_cue_chars": longest_source,
        "max_display_cue_chars": display_max_length,
        "min_word_timing_audio_coverage_ratio": round(min(cue_coverage_ratios), 4),
        "max_subtitle_width_px": round(maximum_width, 2),
        "max_subtitle_height_px": maximum_height,
        "widest_subtitle": widest_text,
        "subtitle_available_width_px": 1920 - 2 * horizontal_margin,
        "frame_mean_absolute_differences_top_safe_area": frame_differences,
    }
    qa_dir = project / "qa"
    qa_dir.mkdir(exist_ok=True)
    result_path = qa_dir / f"media_qa_{output_tag}.json"
    report_path = qa_dir / f"媒体技术检查报告_{output_tag}.md"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(report_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
