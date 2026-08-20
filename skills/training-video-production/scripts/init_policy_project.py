#!/usr/bin/env python3
"""Initialize a traceable one-policy-one-video project without inventing content."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


LINES = ("管家", "工程", "秩序", "绿化", "保洁", "其他")
DIRECTORIES = (
    "sources",
    "extraction",
    "prompts",
    "origin_image",
    "audio",
    "subtitles",
    "video",
    "qa",
    "approvals",
)


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the standard directory and control files for one policy video."
    )
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--line", required=True, choices=LINES)
    args = parser.parse_args()

    project = args.project_dir.expanduser().resolve()
    if project.exists() and any(project.iterdir()):
        raise SystemExit(f"Refusing to initialize non-empty directory: {project}")
    project.mkdir(parents=True, exist_ok=True)
    for name in DIRECTORIES:
        (project / name).mkdir(exist_ok=True)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {
        "package": "policy_training_video",
        "policy_id": args.policy_id,
        "policy_title": args.title,
        "professional_line": args.line,
        "status": "source_registration_pending",
        "current_gate": "formal_source_confirmation",
        "created_at": now,
        "updated_at": now,
        "deck_input": {
            "mode": "native_generation",
            "path": None,
            "sha256": None,
            "dynamic_content_disposition": "not_applicable",
        },
        "voiceover": {
            "default_provider": "melo",
            "selected_provider": None,
            "selected_voice": None,
            "speed": 0.95,
            "voice_confirmation_basis": None,
            "voice_cloning": False,
            "online_fallback": False,
            "output_tag": "V4",
        },
        "sources": [],
        "approvals": {
            "formal_source": "pending",
            "outline": "pending",
            "visual_direction": "pending",
            "image_backend": "pending",
            "sample_slide": "pending",
            "existing_ppt_direct_use": "not_applicable",
            "full_deck_authorization": "pending",
            "deck_and_speech": "pending",
            "video_stage": "pending",
            "local_voice_selection": "pending",
            "online_tts_external_transfer": "not_authorized",
            "policy_owner_final": "pending",
            "frontline_pilot": "pending",
            "release": "pending",
        },
        "checks": {},
        "artifacts": [],
    }
    ledger = {
        "policy_id": args.policy_id,
        "policy_title": args.title,
        "source_sha256": "",
        "source_structure": "extraction/policy_structure.json",
        "decision_references": [],
        "expected_substantive_paragraphs": [],
        "rules": [],
    }
    outline_map = {"policy_id": args.policy_id, "slides": []}

    write_json(project / "manifest.json", manifest)
    write_json(project / "evidence_ledger.json", ledger)
    write_json(project / "outline_rule_map.json", outline_map)

    approval_template = (
        Path(__file__).resolve().parents[1]
        / "assets/templates/审批记录.md"
    ).read_text(encoding="utf-8")
    approval_template = approval_template.replace(
        "- 制度编号：", f"- 制度编号：{args.policy_id}"
    ).replace("- 制度名称：", f"- 制度名称：{args.title}")
    (project / "approvals/审批记录.md").write_text(approval_template, encoding="utf-8")

    print(f"PASS: initialized {project}")
    print("Next gate: register and confirm the formal source before adding policy rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
