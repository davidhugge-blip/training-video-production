#!/usr/bin/env python3
"""Local MeloTTS synthesis and MFA forced-alignment helpers.

The module intentionally keeps Melo imports inside the synthesis entry point so
the main media pipeline can use the alignment and QA helpers without loading
Torch.  Network fallback is disabled by the caller for production synthesis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


MELO_LANGUAGE = "ZH"
MELO_OFFICIAL_SPEAKERS = ("ZH",)
MELO_DEFAULT_SPEED = 0.95
COMPRESSOR = "acompressor=threshold=-20dB:ratio=1.4:attack=20:release=250:makeup=1"
LOUDNESS = {"I": -16.0, "TP": -1.5, "LRA": 7.0}
TERM_PATTERN = re.compile(
    r"[A-Za-z]+(?:[-_][A-Za-z0-9]+)*"
    r"|[A-Z0-9]+(?:-[A-Z0-9]+){2,}"
    r"|\d+(?:\.\d+)?(?:%|％|元|万元|天|日|月|年|次|分|小时|分钟|个|项|条|户|点)?"
)
HAN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_blocking_terms(text: str) -> list[str]:
    """Return Latin and Arabic-number tokens that require approved rewrites."""
    return list(dict.fromkeys(match.group(0) for match in TERM_PATTERN.finditer(text)))


def alignment_units(text: str) -> list[str]:
    """Use one Han character per MFA token for deterministic source mapping."""
    unsupported = [
        character
        for character in text
        if character.isalnum() and not HAN_PATTERN.fullmatch(character)
    ]
    if unsupported:
        unique = "".join(dict.fromkeys(unsupported))
        raise RuntimeError(
            "local MFA alignment accepts approved natural-Chinese narration only; "
            f"unapproved alphanumeric units remain: {unique}"
        )
    units = [character for character in text if HAN_PATTERN.fullmatch(character)]
    if not units:
        raise RuntimeError("narration has no alignable Chinese speech units")
    return units


def parse_loudnorm(stderr: str) -> dict[str, str]:
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr, flags=re.S)
    if not matches:
        raise RuntimeError("FFmpeg loudnorm analysis did not return JSON")
    return json.loads(matches[-1])


def normalize_two_pass(ffmpeg: Path, source: Path, target: Path) -> dict[str, object]:
    first_filter = (
        f"{COMPRESSOR},loudnorm=I={LOUDNESS['I']}:TP={LOUDNESS['TP']}:"
        f"LRA={LOUDNESS['LRA']}:print_format=json"
    )
    first_run = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-i", str(source), "-af", first_filter,
            "-f", "null", "-",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first = parse_loudnorm(first_run.stderr)
    second_filter = (
        f"{COMPRESSOR},loudnorm=I={LOUDNESS['I']}:TP={LOUDNESS['TP']}:"
        f"LRA={LOUDNESS['LRA']}:measured_I={first['input_i']}:"
        f"measured_LRA={first['input_lra']}:measured_TP={first['input_tp']}:"
        f"measured_thresh={first['input_thresh']}:offset={first['target_offset']}:"
        "linear=true:print_format=summary"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_raw = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".wav", dir=target.parent
    )
    os.close(descriptor)
    staged = Path(staged_raw)
    try:
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-i", str(source), "-af",
                second_filter, "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
                str(staged),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if staged.stat().st_size < 10_000:
            raise RuntimeError("normalized MeloTTS WAV is empty")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return {
        "compressor": COMPRESSOR,
        "loudness_target": LOUDNESS,
        "first_pass": first,
        "second_pass_filter": second_filter,
    }


def run_melo_synthesis(
    *,
    python: Path,
    helper_script: Path,
    text_file: Path,
    output: Path,
    speaker: str,
    speed: float,
    melo_source: Path | None,
) -> None:
    command = [
        str(python), str(helper_script), "synthesize", "--text-file", str(text_file),
        "--output", str(output), "--speaker", speaker, "--speed", str(speed),
    ]
    if melo_source is not None:
        command.extend(["--melo-source", str(melo_source)])
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, env=environment)
    if not output.is_file() or output.stat().st_size < 10_000:
        raise RuntimeError("MeloTTS did not produce a valid local WAV")


def run_melo_batch(
    *,
    python: Path,
    helper_script: Path,
    jobs_file: Path,
    speaker: str,
    speed: float,
    melo_source: Path | None,
) -> None:
    command = [
        str(python), str(helper_script), "synthesize-batch", "--jobs-file",
        str(jobs_file), "--speaker", speaker, "--speed", str(speed),
    ]
    if melo_source is not None:
        command.extend(["--melo-source", str(melo_source)])
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    subprocess.run(command, check=True, env=environment)
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
    for job in jobs:
        output = Path(job["output"])
        if not output.is_file() or output.stat().st_size < 10_000:
            raise RuntimeError(f"MeloTTS did not produce a valid local WAV: {output}")


def parse_textgrid_words(path: Path) -> tuple[float, list[dict[str, object]]]:
    source = path.read_text(encoding="utf-8")
    tier_start = source.find('name = "words"')
    if tier_start < 0:
        raise RuntimeError(f"MFA TextGrid has no words tier: {path}")
    next_tier = source.find("\n    item [", tier_start)
    tier = source[tier_start:next_tier] if next_tier >= 0 else source[tier_start:]
    xmax_match = re.search(r"xmax\s*=\s*([0-9.]+)", source)
    if not xmax_match:
        raise RuntimeError(f"MFA TextGrid has no duration: {path}")
    intervals: list[dict[str, object]] = []
    pattern = re.compile(
        r"intervals \[\d+\]:\s*xmin\s*=\s*([0-9.]+)\s*"
        r"xmax\s*=\s*([0-9.]+)\s*text\s*=\s*\"([^\"]*)\"",
        re.S,
    )
    for match in pattern.finditer(tier):
        label = match.group(3).strip()
        if label:
            intervals.append(
                {"start": float(match.group(1)), "end": float(match.group(2)), "text": label}
            )
    if not intervals:
        raise RuntimeError(f"MFA TextGrid contains no aligned words: {path}")
    return float(xmax_match.group(1)), intervals


def run_mfa_alignment(
    *,
    mfa: Path,
    mfa_root: Path,
    pkuseg_home: Path,
    cache_home: Path,
    corpus_dir: Path,
    output_dir: Path,
    dictionary: str,
    acoustic_model: str,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{mfa.parent}{os.pathsep}{environment.get('PATH', '')}",
            "MFA_ROOT_DIR": str(mfa_root),
            "PKUSEG_HOME": str(pkuseg_home),
            "XDG_CACHE_HOME": str(cache_home),
        }
    )
    for directory in (mfa_root, pkuseg_home, cache_home, output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(mfa), "align", str(corpus_dir), dictionary, acoustic_model,
            str(output_dir), "--clean", "--single_speaker", "--num_jobs", "1",
        ],
        check=True,
        env=environment,
    )


def materialize_alignment_input(audio: Path, lab: Path, destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lab.write_text(" ".join(alignment_units(text)) + "\n", encoding="utf-8")
    destination.unlink(missing_ok=True)
    try:
        os.link(audio, destination)
    except OSError:
        shutil.copyfile(audio, destination)


def _load_melo(melo_source: Path | None):
    if melo_source is not None:
        sys.path.insert(0, str(melo_source.resolve()))
    import torch

    torch.backends.mps.is_available = lambda: False
    from melo.api import TTS

    return TTS


def synthesize_cli(args: argparse.Namespace) -> int:
    if args.speaker not in MELO_OFFICIAL_SPEAKERS:
        raise SystemExit(f"unsupported official MeloTTS ZH speaker: {args.speaker}")
    text = args.text_file.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("empty synthesis text")
    TTS = _load_melo(args.melo_source)
    model = TTS(language=MELO_LANGUAGE, device="cpu", use_hf=True)
    speaker_ids = model.hps.data.spk2id
    if args.speaker not in speaker_ids:
        raise SystemExit(
            f"speaker {args.speaker!r} not present in official model; available={list(speaker_ids)}"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model.tts_to_file(
        text, speaker_ids[args.speaker], str(args.output), speed=args.speed, quiet=True
    )
    return 0


def synthesize_batch_cli(args: argparse.Namespace) -> int:
    if args.speaker not in MELO_OFFICIAL_SPEAKERS:
        raise SystemExit(f"unsupported official MeloTTS ZH speaker: {args.speaker}")
    jobs = json.loads(args.jobs_file.read_text(encoding="utf-8"))
    if not isinstance(jobs, list) or not jobs:
        raise SystemExit("batch jobs file must contain a non-empty JSON list")
    TTS = _load_melo(args.melo_source)
    model = TTS(language=MELO_LANGUAGE, device="cpu", use_hf=True)
    speaker_ids = model.hps.data.spk2id
    if args.speaker not in speaker_ids:
        raise SystemExit(
            f"speaker {args.speaker!r} not present in official model; available={list(speaker_ids)}"
        )
    for job in jobs:
        text_file = Path(job["text_file"])
        output = Path(job["output"])
        text = text_file.read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"empty synthesis text: {text_file}")
        output.parent.mkdir(parents=True, exist_ok=True)
        model.tts_to_file(
            text, speaker_ids[args.speaker], str(output), speed=args.speed, quiet=True
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    voices = subparsers.add_parser("list-voices")
    voices.add_argument("--json", action="store_true")
    synthesize = subparsers.add_parser("synthesize")
    synthesize.add_argument("--text-file", type=Path, required=True)
    synthesize.add_argument("--output", type=Path, required=True)
    synthesize.add_argument("--speaker", default="ZH")
    synthesize.add_argument("--speed", type=float, default=MELO_DEFAULT_SPEED)
    synthesize.add_argument("--melo-source", type=Path)
    batch = subparsers.add_parser("synthesize-batch")
    batch.add_argument("--jobs-file", type=Path, required=True)
    batch.add_argument("--speaker", default="ZH")
    batch.add_argument("--speed", type=float, default=MELO_DEFAULT_SPEED)
    batch.add_argument("--melo-source", type=Path)
    args = parser.parse_args()
    if args.command == "list-voices":
        payload = {
            "provider": "MeloTTS",
            "language": MELO_LANGUAGE,
            "official_fixed_voices": list(MELO_OFFICIAL_SPEAKERS),
            "voice_cloning": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "\n".join(MELO_OFFICIAL_SPEAKERS))
        return 0
    if args.command == "synthesize":
        return synthesize_cli(args)
    return synthesize_batch_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
