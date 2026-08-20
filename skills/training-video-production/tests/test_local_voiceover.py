from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import media_pipeline as media  # noqa: E402
import melo_mfa_voiceover as local  # noqa: E402


class LocalVoiceoverTests(unittest.TestCase):
    def test_term_gate_blocks_latin_policy_id_and_arabic_numbers(self) -> None:
        text = "文件编号ABC-HR-RULE-001，活动在21点前结束，生成SRT字幕。"
        terms = local.scan_blocking_terms(text)
        self.assertIn("ABC-HR-RULE-001", terms)
        self.assertIn("21点", terms)
        self.assertIn("SRT", terms)

    def test_alignment_units_are_exact_han_characters(self) -> None:
        self.assertEqual(local.alignment_units("你好，世界。"), list("你好世界"))
        with self.assertRaises(RuntimeError):
            local.alignment_units("第三版V3")

    def test_parse_textgrid_words_reads_actual_intervals(self) -> None:
        textgrid = """File type = "ooTextFile"
Object class = "TextGrid"
xmin = 0
xmax = 1.2
item []:
    item [1]:
        class = "IntervalTier"
        name = "words"
        xmin = 0
        xmax = 1.2
        intervals: size = 3
        intervals [1]:
            xmin = 0
            xmax = 0.2
            text = ""
        intervals [2]:
            xmin = 0.2
            xmax = 0.6
            text = "你"
        intervals [3]:
            xmin = 0.6
            xmax = 1.0
            text = "好"
    item [2]:
        class = "IntervalTier"
        name = "phones"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.TextGrid"
            path.write_text(textgrid, encoding="utf-8")
            duration, intervals = local.parse_textgrid_words(path)
        self.assertEqual(duration, 1.2)
        self.assertEqual([entry["text"] for entry in intervals], ["你", "好"])
        self.assertEqual(intervals[0]["start"], 0.2)

    def test_local_term_gate_writes_fail_closed_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            slides = [media.SlideSpeech(1, "测试", "编号是ABC-001。")]
            path, report = media.write_local_term_gate(project, slides, output_tag="TEST")
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "blocked_pending_approved_natural_chinese_rewrite")
        self.assertGreater(stored["blocking_term_count"], 0)

    def test_mfa_character_boundaries_map_back_to_exact_approved_text(self) -> None:
        text = "先检查，再复核。"
        units = local.alignment_units(text)
        boundaries = [
            {
                "text": unit,
                "offset": 1_000_000 + index * 2_000_000,
                "duration": 2_000_000,
            }
            for index, unit in enumerate(units)
        ]
        cues = media.subtitle_cues(
            text,
            boundaries,
            measure_text=lambda value: len(value) * 40.0,
            target_width_px=500,
            hard_width_px=600,
            preferred_max_seconds=7.0,
        )
        self.assertEqual("".join(cue.text for cue in cues), text)


if __name__ == "__main__":
    unittest.main()
