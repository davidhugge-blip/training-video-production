#!/usr/bin/env python3
"""Validate progressively stricter gates in a one-policy-one-video project."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


REQUIRED_RULE_FIELDS = (
    "rule_id",
    "source_pages",
    "source_paragraphs",
    "article",
    "category",
    "normalized_rule",
    "source_text",
    "training_treatment",
)
ALLOWED_TREATMENTS = {"include", "mention", "exclude"}
ALLOWED_DECK_MODES = {"native_generation", "existing_finished_ppt"}


def load_json(path: Path, errors: list[str]) -> dict:
    if not path.is_file():
        errors.append(f"missing file: {path.name}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f"invalid JSON {path.name}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"JSON root must be an object: {path.name}")
        return {}
    return value


def validate_manifest(project: Path, errors: list[str]) -> dict:
    manifest = load_json(project / "manifest.json", errors)
    for field in ("policy_id", "policy_title", "professional_line", "sources", "approvals"):
        if field not in manifest or manifest[field] in (None, ""):
            errors.append(f"manifest missing field: {field}")
    sources = manifest.get("sources", [])
    if not sources:
        errors.append("manifest has no registered source")
    primary = [item for item in sources if item.get("role") == "primary_policy"]
    if len(primary) != 1:
        errors.append(f"manifest must contain exactly one primary_policy source, found {len(primary)}")
    for item in sources:
        for field in ("path", "role", "sha256", "bytes", "pages"):
            if not item.get(field):
                errors.append(f"source missing {field}: {item.get('path', '<unknown>')}")
        source_path = project / str(item.get("path", ""))
        if item.get("path") and not source_path.is_file():
            errors.append(f"registered source does not exist: {item['path']}")
        elif source_path.is_file():
            actual_bytes = source_path.stat().st_size
            if item.get("bytes") != actual_bytes:
                errors.append(
                    f"source byte count drift for {item.get('path')}: "
                    f"recorded {item.get('bytes')}, actual {actual_bytes}"
                )
            actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if item.get("sha256") != actual_hash:
                errors.append(
                    f"source SHA-256 drift for {item.get('path')}: "
                    f"recorded {item.get('sha256')}, actual {actual_hash}"
                )
    deck_input = manifest.get("deck_input", {"mode": "native_generation"})
    if not isinstance(deck_input, dict):
        errors.append("manifest deck_input must be an object")
    else:
        deck_mode = deck_input.get("mode", "native_generation")
        if deck_mode not in ALLOWED_DECK_MODES:
            errors.append(f"invalid deck_input mode: {deck_mode!r}")
        if deck_mode == "existing_finished_ppt":
            deck_path_value = deck_input.get("path")
            if not deck_path_value:
                errors.append("existing_finished_ppt mode requires deck_input.path")
            else:
                deck_path = project / str(deck_path_value)
                if deck_path.suffix.lower() != ".pptx":
                    errors.append("existing finished PPT must be a reviewable .pptx file")
                if not deck_path.is_file():
                    errors.append(f"existing finished PPT does not exist: {deck_path_value}")
                elif deck_input.get("sha256") != hashlib.sha256(deck_path.read_bytes()).hexdigest():
                    errors.append("existing finished PPT SHA-256 differs from manifest")
    return manifest


def validate_deck_input_gate(manifest: dict, errors: list[str]) -> None:
    deck_input = manifest.get("deck_input", {"mode": "native_generation"})
    if not isinstance(deck_input, dict) or deck_input.get("mode") != "existing_finished_ppt":
        return
    approvals = manifest.get("approvals", {})
    approval = approvals.get("existing_ppt_direct_use") if isinstance(approvals, dict) else None
    if approval != "approved":
        errors.append("existing finished PPT direct use is not approved")
    disposition = deck_input.get("dynamic_content_disposition")
    if disposition not in {"none_detected", "approved_static"}:
        errors.append(
            "existing finished PPT dynamic content disposition must be "
            "none_detected or approved_static"
        )


def validate_facts(project: Path, errors: list[str]) -> tuple[dict, dict]:
    manifest = validate_manifest(project, errors)
    ledger = load_json(project / "evidence_ledger.json", errors)
    rules = ledger.get("rules", [])
    if not rules:
        errors.append("evidence ledger has no rules")
        return manifest, ledger
    if ledger.get("policy_id") != manifest.get("policy_id"):
        errors.append("policy_id differs between manifest and evidence ledger")
    primary = next(
        (item for item in manifest.get("sources", []) if item.get("role") == "primary_policy"),
        None,
    )
    if primary and ledger.get("source_sha256") != primary.get("sha256"):
        errors.append("primary source SHA-256 differs between manifest and evidence ledger")

    structure_path = project / str(ledger.get("source_structure", ""))
    paragraph_map: dict[int, str] = {}
    if structure_path.is_file():
        structure = load_json(structure_path, errors)
        paragraph_map = {
            item["paragraph_index"]: item["text"]
            for item in structure.get("paragraphs", [])
            if "paragraph_index" in item and "text" in item
        }
    else:
        errors.append(f"source structure does not exist: {ledger.get('source_structure', '')}")

    seen: set[str] = set()
    covered: set[int] = set()
    for rule in rules:
        rule_id = str(rule.get("rule_id", "<missing>"))
        if rule_id in seen:
            errors.append(f"duplicate rule_id: {rule_id}")
        seen.add(rule_id)
        for field in REQUIRED_RULE_FIELDS:
            if field not in rule or rule[field] in (None, "", []):
                errors.append(f"{rule_id}: missing field {field}")
        treatment = rule.get("training_treatment")
        if treatment not in ALLOWED_TREATMENTS:
            errors.append(f"{rule_id}: invalid training_treatment {treatment!r}")
        if treatment == "exclude" and not rule.get("exclusion_reason"):
            errors.append(f"{rule_id}: excluded rule needs exclusion_reason")

        paragraphs = rule.get("source_paragraphs", [])
        covered.update(index for index in paragraphs if isinstance(index, int))
        source_text = rule.get("source_text", [])
        combined = " ".join(source_text)
        for token in rule.get("risk_tokens", []):
            if str(token).replace(" ", "") not in combined.replace(" ", ""):
                errors.append(f"{rule_id}: risk token absent from source_text: {token}")
        if paragraph_map:
            actual = [paragraph_map.get(index) for index in paragraphs]
            if None in actual:
                errors.append(f"{rule_id}: source paragraph link missing")
            elif actual != source_text:
                errors.append(f"{rule_id}: source_text drift from parsed structure")

    expected = set(ledger.get("expected_substantive_paragraphs", []))
    if not expected:
        errors.append("expected_substantive_paragraphs is empty")
    elif expected != covered:
        if expected - covered:
            errors.append(f"uncovered substantive paragraphs: {sorted(expected - covered)}")
        if covered - expected:
            errors.append(f"unexpected covered paragraphs: {sorted(covered - expected)}")
    return manifest, ledger


def validate_outline(project: Path, ledger: dict, errors: list[str]) -> dict:
    outline = load_json(project / "outline_rule_map.json", errors)
    slides = outline.get("slides", [])
    if not slides:
        errors.append("outline_rule_map has no slides")
        return outline
    expected_numbers = list(range(1, len(slides) + 1))
    actual_numbers = [slide.get("slide") for slide in slides]
    if actual_numbers != expected_numbers:
        errors.append(f"slide numbers must be continuous: expected {expected_numbers}, got {actual_numbers}")

    rules = {rule["rule_id"]: rule for rule in ledger.get("rules", [])}
    required = {
        rule_id for rule_id, rule in rules.items()
        if rule.get("training_treatment") != "exclude"
    }
    mapped: dict[str, int] = {}
    for slide in slides:
        if not slide.get("title"):
            errors.append(f"slide {slide.get('slide')}: missing title")
        for rule_id in slide.get("rule_ids", []):
            if rule_id not in rules:
                errors.append(f"slide {slide.get('slide')}: unknown rule_id {rule_id}")
            elif rules[rule_id].get("training_treatment") == "exclude":
                errors.append(f"slide {slide.get('slide')}: excluded rule mapped {rule_id}")
            if rule_id in mapped:
                errors.append(f"rule {rule_id} mapped to multiple slides")
            mapped[rule_id] = slide.get("slide")
    if required - set(mapped):
        errors.append(f"unmapped rules: {sorted(required - set(mapped))}")
    return outline


def speech_slides(path: Path, errors: list[str]) -> list[int]:
    if not path.is_file():
        errors.append("missing file: speech.md")
        return []
    source = path.read_text(encoding="utf-8")
    numbers = [int(value) for value in re.findall(r"^## Slide\s+(\d+):", source, re.MULTILINE)]
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append(f"speech slide headings are not continuous: {numbers}")
    return numbers


def preferred_versioned_json(
    project: Path,
    stem: str,
    output_tag: str | None = None,
) -> Path | None:
    """Return an explicitly selected artifact, then prefer current locked tags."""
    qa_dir = project / "qa"
    if output_tag:
        if any(character in output_tag for character in "/\\\n\r"):
            return None
        selected = qa_dir / f"{stem}_{output_tag}.json"
        return selected if selected.is_file() else None
    for tag in ("V4", "V3", "V2"):
        locked = qa_dir / f"{stem}_{tag}.json"
        if locked.is_file():
            return locked
    tagged = sorted(
        qa_dir.glob(f"{stem}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if tagged:
        return tagged[0]
    legacy = qa_dir / f"{stem}.json"
    return legacy if legacy.is_file() else None


def validate_deck(project: Path, outline: dict, errors: list[str]) -> int:
    slide_count = len(outline.get("slides", []))
    numbers = speech_slides(project / "speech.md", errors)
    if len(numbers) != slide_count:
        errors.append(f"speech slide count {len(numbers)} != outline slide count {slide_count}")
    images = sorted((project / "origin_image").glob("slide_*.png"))
    if len(images) != slide_count:
        errors.append(f"slide image count {len(images)} != outline slide count {slide_count}")
    pptx_files = sorted(project.glob("*.pptx"))
    if not pptx_files:
        errors.append("no PPTX found in project root")
    if not (project / "deck_spec.json").is_file():
        errors.append("missing file: deck_spec.json")
    source_maps = (
        project / "逐页来源映射.md",
        project / "speech_source_map.json",
    )
    if not any(path.is_file() for path in source_maps):
        errors.append("missing slide source map: 逐页来源映射.md or speech_source_map.json")
    return slide_count


def validate_media(
    project: Path,
    slide_count: int,
    errors: list[str],
    *,
    output_tag: str | None = None,
) -> None:
    media_manifest_path = preferred_versioned_json(
        project,
        "media_manifest",
        output_tag,
    )
    media_manifest: dict = {}
    if media_manifest_path is None:
        suffix = f"_{output_tag}" if output_tag else "[_TAG]"
        errors.append(f"missing file: qa/media_manifest{suffix}.json")
    else:
        media_manifest = load_json(media_manifest_path, errors)

    recorded_slide_count = media_manifest.get("slide_count", media_manifest.get("slides"))
    if recorded_slide_count is not None and recorded_slide_count != slide_count:
        errors.append(
            f"media manifest slide count {recorded_slide_count} != slide count {slide_count}"
        )

    audio_info = media_manifest.get("audio", {})
    provider = str(audio_info.get("provider", "")) if isinstance(audio_info, dict) else ""
    if provider.startswith("MeloTTS local"):
        audio_extension = "wav"
    elif provider == "Microsoft Edge online TTS":
        audio_extension = "mp3"
    else:
        wav = sorted((project / "audio").glob("slide_*.wav"))
        mp3 = sorted((project / "audio").glob("slide_*.mp3"))
        audio_extension = "wav" if len(wav) == slide_count and not mp3 else "mp3"

    audio = sorted((project / "audio").glob(f"slide_*.{audio_extension}"))
    subtitles = sorted((project / "subtitles").glob("slide_*.srt"))
    videos = sorted((project / "video").glob("*.mp4"))
    if len(audio) != slide_count:
        errors.append(
            f"{audio_extension.upper()} audio count {len(audio)} != slide count {slide_count}"
        )
    if len(subtitles) != slide_count:
        errors.append(f"per-slide subtitle count {len(subtitles)} != slide count {slide_count}")
    if not videos:
        errors.append("no final MP4 found")
    media_qa_path = preferred_versioned_json(project, "media_qa", output_tag)
    if media_qa_path is None:
        suffix = f"_{output_tag}" if output_tag else "[_TAG]"
        errors.append(f"missing file: qa/media_qa{suffix}.json")
    else:
        qa_result = load_json(media_qa_path, errors)
        if qa_result.get("status") != "pass":
            errors.append(f"media QA is not pass: {media_qa_path.name}")
        manifest_pipeline = media_manifest.get("pipeline_version")
        qa_pipeline = qa_result.get("pipeline_version")
        if manifest_pipeline and qa_pipeline and manifest_pipeline != qa_pipeline:
            errors.append(
                f"media pipeline version differs between manifest and QA: "
                f"{manifest_pipeline} != {qa_pipeline}"
            )

    if provider.startswith("MeloTTS local"):
        project_manifest = load_json(project / "manifest.json", errors)
        approvals = project_manifest.get("approvals", {})
        voiceover = project_manifest.get("voiceover", {})
        if not isinstance(approvals, dict) or approvals.get("local_voice_selection") != "approved":
            errors.append("local official voice selection is not approved")
        if not isinstance(voiceover, dict):
            errors.append("manifest voiceover must be an object")
        else:
            expected_voice = audio_info.get("speaker") if isinstance(audio_info, dict) else None
            expected_speed = audio_info.get("speed") if isinstance(audio_info, dict) else None
            if voiceover.get("selected_provider") != "melo":
                errors.append("manifest selected voice provider is not melo")
            if voiceover.get("selected_voice") != expected_voice:
                errors.append("manifest selected voice differs from media manifest")
            if voiceover.get("speed") != expected_speed:
                errors.append("manifest voice speed differs from media manifest")
            if not str(voiceover.get("voice_confirmation_basis", "")).strip():
                errors.append("manifest local voice confirmation basis is missing")
            if voiceover.get("voice_cloning") is not False:
                errors.append("manifest must record local voice cloning as disabled")
            if voiceover.get("online_fallback") is not False:
                errors.append("manifest must record online fallback as disabled")
        if isinstance(audio_info, dict):
            governance = (
                audio_info.get("voice_confirmed") is True
                and bool(str(audio_info.get("voice_confirmation_basis", "")).strip())
                and audio_info.get("voice_cloning") is False
                and audio_info.get("local_only") is True
                and audio_info.get("online_external_transfer_authorized") is False
                and audio_info.get("network_fallback") is False
            )
            if not governance:
                errors.append("local media manifest governance record is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--stage", choices=("facts", "outline", "deck", "media"), required=True)
    parser.add_argument("--output-tag", help="exact media manifest and QA tag for the media gate")
    args = parser.parse_args()
    project = args.project_dir.expanduser().resolve()
    errors: list[str] = []

    manifest, ledger = validate_facts(project, errors)
    outline: dict = {}
    slide_count = 0
    if args.stage in {"outline", "deck", "media"}:
        outline = validate_outline(project, ledger, errors)
    if args.stage in {"deck", "media"}:
        validate_deck_input_gate(manifest, errors)
        slide_count = validate_deck(project, outline, errors)
    if args.stage == "media":
        validate_media(project, slide_count, errors, output_tag=args.output_tag)

    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    print(f"PASS: {args.stage} gate validated for {project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
