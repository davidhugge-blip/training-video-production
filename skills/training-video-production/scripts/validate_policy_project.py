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
    return manifest


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


def preferred_versioned_json(project: Path, stem: str) -> Path | None:
    """Return the locked V2 artifact when present, then a tagged or legacy file."""
    qa_dir = project / "qa"
    v2 = qa_dir / f"{stem}_V2.json"
    if v2.is_file():
        return v2
    tagged = sorted(qa_dir.glob(f"{stem}_*.json"))
    if tagged:
        return tagged[-1]
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


def validate_media(project: Path, slide_count: int, errors: list[str]) -> None:
    audio = sorted((project / "audio").glob("slide_*.mp3"))
    subtitles = sorted((project / "subtitles").glob("slide_*.srt"))
    videos = sorted((project / "video").glob("*.mp4"))
    if len(audio) != slide_count:
        errors.append(f"audio count {len(audio)} != slide count {slide_count}")
    if len(subtitles) != slide_count:
        errors.append(f"per-slide subtitle count {len(subtitles)} != slide count {slide_count}")
    if not videos:
        errors.append("no final MP4 found")
    media_manifest = preferred_versioned_json(project, "media_manifest")
    if media_manifest is None:
        errors.append("missing file: qa/media_manifest[_TAG].json")
    media_qa = preferred_versioned_json(project, "media_qa")
    if media_qa is None:
        errors.append("missing file: qa/media_qa[_TAG].json")
    else:
        qa_result = load_json(media_qa, errors)
        if qa_result.get("status") != "pass":
            errors.append(f"media QA is not pass: {media_qa.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--stage", choices=("facts", "outline", "deck", "media"), required=True)
    args = parser.parse_args()
    project = args.project_dir.expanduser().resolve()
    errors: list[str] = []

    _, ledger = validate_facts(project, errors)
    outline: dict = {}
    slide_count = 0
    if args.stage in {"outline", "deck", "media"}:
        outline = validate_outline(project, ledger, errors)
    if args.stage in {"deck", "media"}:
        slide_count = validate_deck(project, outline, errors)
    if args.stage == "media":
        validate_media(project, slide_count, errors)

    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    print(f"PASS: {args.stage} gate validated for {project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
