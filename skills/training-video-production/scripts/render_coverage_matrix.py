#!/usr/bin/env python3
"""Validate and render the generic rule-to-slide coverage matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    project = args.project_dir.expanduser().resolve()
    ledger = json.loads((project / "evidence_ledger.json").read_text(encoding="utf-8"))
    outline = json.loads((project / "outline_rule_map.json").read_text(encoding="utf-8"))
    rules = {item["rule_id"]: item for item in ledger.get("rules", [])}
    if not rules:
        print("ERROR: evidence ledger has no rules")
        return 1
    required = {
        rule_id
        for rule_id, rule in rules.items()
        if rule.get("training_treatment") != "exclude"
    }
    excluded = set(rules) - required

    mapped: dict[str, int] = {}
    slide_map: dict[int, dict[str, object]] = {}
    errors: list[str] = []
    for slide in outline.get("slides", []):
        number = slide.get("slide")
        if not isinstance(number, int) or number < 1:
            errors.append(f"invalid slide number: {number!r}")
            continue
        if number in slide_map:
            errors.append(f"duplicate slide number: {number}")
        slide_map[number] = slide
        for rule_id in slide.get("rule_ids", []):
            if rule_id not in rules:
                errors.append(f"unknown rule_id on slide {number}: {rule_id}")
            elif rule_id in excluded:
                errors.append(f"excluded rule mapped to slide {number}: {rule_id}")
            if rule_id in mapped:
                errors.append(
                    f"duplicate primary mapping for {rule_id}: slides {mapped[rule_id]} and {number}"
                )
            mapped[rule_id] = number

    missing = sorted(required - set(mapped))
    if missing:
        errors.append(f"unmapped rules: {missing}")
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1

    lines = [
        "# 规则覆盖矩阵",
        "",
        f"- 制度编号：`{ledger.get('policy_id', '')}`",
        f"- 规则总数：{len(rules)}",
        f"- 纳入或提及：{len(required)}",
        f"- 排除：{len(excluded)}",
        "- 覆盖状态：全部纳入规则均有唯一主页面；总结页不得新增制度规则。",
        "",
        "| 规则编号 | 类别 | 制度条款及页码 | 课件处理 | 主页面 | 页面标题 |",
        "|---|---|---|---|---:|---|",
    ]
    for rule in ledger.get("rules", []):
        rule_id = rule["rule_id"]
        pages = ",".join(str(page) for page in rule.get("source_pages", []))
        if rule_id in excluded:
            slide_number = "—"
            title = "课件外（保留排除原因）"
        else:
            number = mapped[rule_id]
            slide_number = str(number)
            title = str(slide_map[number].get("title", ""))
        lines.append(
            f"| `{rule_id}` | {rule.get('category', '')} | "
            f"{rule.get('article', '')}，第{pages or '—'}页 | "
            f"{rule.get('training_treatment', '')} | {slide_number} | {title} |"
        )

    if excluded:
        lines.extend(["", "## 课件外规则", ""])
        for rule_id in sorted(excluded):
            reason = rules[rule_id].get("exclusion_reason", "未填写排除原因")
            lines.append(f"- `{rule_id}`：{reason}")

    output = args.output.resolve() if args.output else project / "规则覆盖矩阵.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"PASS: {len(required)} included rules mapped to {len(slide_map)} slides: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
