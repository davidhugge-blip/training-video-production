from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import media_pipeline as media  # noqa: E402
import qa_media as qa  # noqa: E402


def width(text: str) -> float:
    return sum(not character.isspace() for character in text) * 40.0


def boundaries(text: str, tokens: list[str]) -> list[dict[str, object]]:
    assert media.text_key(text) == "".join(media.text_key(token) for token in tokens)
    result: list[dict[str, object]] = []
    cursor = 1_000_000
    for token in tokens:
        duration = max(2_000_000, len(media.text_key(token)) * 800_000)
        result.append({"text": token, "offset": cursor, "duration": duration})
        cursor += duration
    return result


class SemanticSubtitleTests(unittest.TestCase):
    def test_approved_local_workflow_is_the_default(self) -> None:
        self.assertEqual(media.DEFAULT_TTS_PROVIDER, "melo")
        self.assertEqual(media.DEFAULT_OUTPUT_TAG, "V4")
        self.assertEqual(media.PIPELINE_VERSION, "4.1")

    def make_slide_project(self, dimensions: list[tuple[int, int]]) -> tuple[tempfile.TemporaryDirectory[str], Path, list[media.SlideSpeech]]:
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name)
        image_dir = project / "origin_image"
        image_dir.mkdir()
        slides: list[media.SlideSpeech] = []
        for number, size in enumerate(dimensions, 1):
            Image.new("RGB", size, "white").save(image_dir / f"slide_{number:02d}.png")
            slides.append(media.SlideSpeech(number, f"页面{number}", "测试讲稿。"))
        return temporary, project, slides

    def test_complete_sentence_is_not_cut_at_twenty_characters(self) -> None:
        text = "今天我们完整学习《示例集团员工行为考核细则》。"
        cues = media.subtitle_cues(
            text,
            boundaries(text, ["今天", "我们", "完整", "学习", "示例集团员工行为考核细则"]),
            measure_text=width,
            target_width_px=1500,
            hard_width_px=1600,
            preferred_max_seconds=7.0,
        )
        self.assertEqual([cue.text for cue in cues], [text])

    def test_long_sentence_splits_at_semantic_punctuation(self) -> None:
        text = "这一页先讲职责范围；然后说明审批顺序，最后提醒大家不要遗漏记录。"
        cues = media.subtitle_cues(
            text,
            boundaries(
                text,
                ["这一页", "先讲", "职责范围", "然后", "说明", "审批顺序", "最后", "提醒", "大家", "不要", "遗漏", "记录"],
            ),
            measure_text=width,
            target_width_px=520,
            hard_width_px=640,
            preferred_max_seconds=7.0,
        )
        rendered = [cue.text for cue in cues]
        self.assertGreater(len(rendered), 1)
        self.assertEqual(media.compact_text("".join(rendered)), media.compact_text(text))
        self.assertTrue(any(part.endswith("；") for part in rendered[:-1]))
        self.assertFalse(any(part[0] in media.LEFT_BOUND_PUNCTUATION for part in rendered))
        self.assertFalse(any(part[-1] in media.OPENING_PUNCTUATION for part in rendered))

    def test_policy_identifier_remains_indivisible(self) -> None:
        text = "制度编号为ABC-HR-RULE-001，请准确记录。"
        cues = media.subtitle_cues(
            text,
            boundaries(text, ["制度", "编号", "为", "ABC", "HR", "RULE", "001", "请", "准确", "记录"]),
            measure_text=width,
            target_width_px=360,
            hard_width_px=760,
            preferred_max_seconds=7.0,
        )
        self.assertTrue(any("ABC-HR-RULE-001" in cue.text for cue in cues))

    def test_existing_cues_can_be_reflowed_without_new_audio(self) -> None:
        old = [
            media.SubtitleCue(0.10, 3.16, "今天我们完整学习《示例集团员工行为"),
            media.SubtitleCue(3.16, 6.06, "考核细则》。"),
        ]
        new = media.reflow_existing_cues(
            old,
            measure_text=width,
            target_width_px=1500,
            hard_width_px=1600,
            preferred_max_seconds=7.0,
        )
        self.assertEqual([cue.text for cue in new], ["今天我们完整学习《示例集团员工行为考核细则》。"])
        self.assertEqual((new[0].start, new[0].end), (0.10, 6.06))

    def test_leading_enumeration_comma_moves_to_previous_semantic_unit(self) -> None:
        old = [
            media.SubtitleCue(0.10, 2.00, "需要留意：≥5000元、3个工作日"),
            media.SubtitleCue(2.00, 4.00, "、24小时、累计5次。"),
        ]
        new = media.reflow_existing_cues(
            old,
            measure_text=width,
            target_width_px=1500,
            hard_width_px=1600,
            preferred_max_seconds=7.0,
        )
        self.assertFalse(any(cue.text.startswith("、") for cue in new))
        self.assertEqual(media.compact_text("".join(cue.text for cue in new)), media.compact_text("".join(cue.text for cue in old)))

    def test_dangling_connector_moves_to_following_semantic_unit(self) -> None:
        old = [
            media.SubtitleCue(0.10, 2.00, "不确定时先停、先"),
            media.SubtitleCue(2.00, 4.00, "问、先审批。"),
        ]
        new = media.reflow_existing_cues(
            old,
            measure_text=width,
            target_width_px=760,
            hard_width_px=900,
            preferred_max_seconds=7.0,
        )
        rendered = [cue.text for cue in new]
        self.assertFalse(any(text.endswith("先") for text in rendered))
        self.assertTrue(any("先问" in text for text in rendered))
        self.assertEqual(media.compact_text("".join(rendered)), media.compact_text("".join(cue.text for cue in old)))

    def test_reflow_repairs_percent_sign_split_across_old_cues(self) -> None:
        old = [
            media.SubtitleCue(15.618, 19.013, "不同条款分别适用的处理结果包括："),
            media.SubtitleCue(19.013, 23.408, "扣除绩效20%、扣除绩效50"),
            media.SubtitleCue(23.408, 26.671, "%、扣除绩效100%、赔偿或承担"),
            media.SubtitleCue(26.671, 30.618, "损失、解除劳动关系或劳动合同。"),
        ]
        new = media.reflow_existing_cues(
            old,
            measure_text=width,
            target_width_px=1500,
            hard_width_px=1600,
            preferred_max_seconds=7.0,
        )
        rendered = [cue.text for cue in new]
        self.assertFalse(any(text.startswith(("%", "％")) for text in rendered))
        self.assertTrue(any("扣除绩效50%" in text for text in rendered))
        self.assertEqual(media.compact_text("".join(rendered)), media.compact_text("".join(cue.text for cue in old)))

    def test_qa_rejects_orphan_punctuation_and_protected_split(self) -> None:
        punctuation_issues = qa.subtitle_structure_issues(
            ["规则内容", "、"],
            measure_text=width,
            hard_width_px=1600,
        )
        self.assertIn("punctuation_only", punctuation_issues)
        protected_issues = qa.subtitle_structure_issues(
            ["学习《员工行为", "考核细则》。"],
            measure_text=width,
            hard_width_px=1600,
        )
        self.assertIn("protected_span_split", protected_issues)
        connector_issues = qa.subtitle_structure_issues(
            ["不确定时先停、先", "问、先审批。"],
            measure_text=width,
            hard_width_px=1600,
        )
        self.assertIn("dangling_connector", connector_issues)

    def test_qa_accepts_nominal_thirty_fps_rounding(self) -> None:
        self.assertTrue(qa.is_nominal_30fps("1920x1080, 401 kb/s, 29.99 fps, 30 tbr"))
        self.assertFalse(qa.is_nominal_30fps("1920x1080, 25 fps, 25 tbr"))

    def test_native_canvas_keeps_locked_16_by_9_geometry(self) -> None:
        temporary, project, slides = self.make_slide_project([(1672, 941)])
        self.addCleanup(temporary.cleanup)
        canvas = media.build_canvas_spec(project, slides, media.NATIVE_DECK_MODE)
        self.assertEqual(canvas.resolution, "1920x1080")
        self.assertEqual((canvas.band_y, canvas.band_height, canvas.font_size), (990, 90, 44))

    def test_existing_ppt_canvas_appends_proportional_band(self) -> None:
        temporary, project, slides = self.make_slide_project([(1440, 1080), (1440, 1080)])
        self.addCleanup(temporary.cleanup)
        canvas = media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)
        self.assertEqual((canvas.slide_width, canvas.slide_height), (1440, 1080))
        self.assertEqual((canvas.video_width, canvas.video_height), (1440, 1170))
        self.assertEqual((canvas.band_y, canvas.band_height, canvas.font_size), (1080, 90, 44))
        self.assertEqual((canvas.target_width_px, canvas.hard_width_px), (1125, 1200))

    def test_existing_ppt_canvas_uses_padding_not_stretching_for_odd_width(self) -> None:
        temporary, project, slides = self.make_slide_project([(1001, 751)])
        self.addCleanup(temporary.cleanup)
        canvas = media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)
        self.assertEqual(canvas.slide_width, 1001)
        self.assertEqual(canvas.video_width, 1002)
        self.assertEqual(canvas.band_y, 751)
        self.assertEqual(canvas.video_height % 2, 0)

    def test_existing_ppt_rejects_inconsistent_rendered_slide_sizes(self) -> None:
        temporary, project, slides = self.make_slide_project([(1440, 1080), (1920, 1080)])
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "one common size"):
            media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)

    def test_pptx_dynamic_content_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pptx = Path(directory) / "dynamic.pptx"
            with zipfile.ZipFile(pptx, "w") as archive:
                archive.writestr(
                    "ppt/slides/slide1.xml",
                    '<p:sld xmlns:p="urn:test"><p:transition/><p:timing/></p:sld>',
                )
                archive.writestr("ppt/media/video1.mp4", b"video")
                archive.writestr("ppt/media/audio1.mp3", b"audio")
            result = media.inspect_pptx_dynamic_content(pptx)
            self.assertEqual(result["transitions"], ["ppt/slides/slide1.xml"])
            self.assertEqual(result["animations"], ["ppt/slides/slide1.xml"])
            self.assertEqual(result["embedded_video"], ["ppt/media/video1.mp4"])
            self.assertEqual(result["embedded_audio"], ["ppt/media/audio1.mp3"])

    def test_existing_ppt_preflight_verifies_hash_ratio_and_approval(self) -> None:
        temporary, project, slides = self.make_slide_project([(1440, 1080)])
        self.addCleanup(temporary.cleanup)
        pptx = project / "approved.pptx"
        with zipfile.ZipFile(pptx, "w") as archive:
            archive.writestr(
                "ppt/presentation.xml",
                '<p:presentation xmlns:p="urn:test"><p:sldSz cx="12192000" cy="9144000"/></p:presentation>',
            )
            archive.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="urn:test"/>')
        canvas = media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)
        manifest = {
            "deck_input": {
                "mode": media.EXISTING_PPT_DECK_MODE,
                "path": "approved.pptx",
                "sha256": media.sha256(pptx),
                "dynamic_content_disposition": "none_detected",
            },
            "approvals": {"existing_ppt_direct_use": "approved"},
        }
        result = media.validate_existing_ppt_preflight(project, manifest, canvas)
        self.assertEqual(result["pptx_page_ratio"], 1.33333333)
        self.assertEqual(result["rendered_page_ratio"], 1.33333333)

    def test_existing_ppt_preflight_stops_on_unapproved_animation(self) -> None:
        temporary, project, slides = self.make_slide_project([(1440, 1080)])
        self.addCleanup(temporary.cleanup)
        pptx = project / "animated.pptx"
        with zipfile.ZipFile(pptx, "w") as archive:
            archive.writestr(
                "ppt/presentation.xml",
                '<p:presentation xmlns:p="urn:test"><p:sldSz cx="12192000" cy="9144000"/></p:presentation>',
            )
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<p:sld xmlns:p="urn:test"><p:timing/></p:sld>',
            )
        canvas = media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)
        manifest = {
            "deck_input": {
                "mode": media.EXISTING_PPT_DECK_MODE,
                "path": "animated.pptx",
                "sha256": media.sha256(pptx),
                "dynamic_content_disposition": "none_detected",
            },
            "approvals": {"existing_ppt_direct_use": "approved"},
        }
        with self.assertRaisesRegex(ValueError, "contains animation"):
            media.validate_existing_ppt_preflight(project, manifest, canvas)

    def test_qa_reads_dynamic_video_dimensions(self) -> None:
        probe = "Stream #0:0: Video: h264, yuv420p, 1440x1170, 30 fps"
        self.assertEqual(qa.probe_dimensions(probe), (1440, 1170))

    def test_existing_ppt_ffmpeg_appends_band_below_slide(self) -> None:
        temporary, project, slides = self.make_slide_project([(1440, 1080)])
        self.addCleanup(temporary.cleanup)
        for name in ("audio", "subtitles", "video", "qa"):
            (project / name).mkdir(exist_ok=True)
        Image.new("RGB", (1440, 1080), (12, 34, 56)).save(
            project / "origin_image/slide_01.png"
        )
        try:
            ffmpeg = media.find_ffmpeg(None)
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        media.run(
            [
                str(ffmpeg), "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000", "-t", "2.0",
                "-c:a", "mp3", str(project / "audio/slide_01.mp3"),
            ]
        )
        (project / "subtitles/slide_01.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,600\n测试讲稿。\n",
            encoding="utf-8",
        )
        (project / "speech.md").write_text(
            "# 逐页讲稿\n\n## Slide 1: 页面1\n\n测试讲稿。\n",
            encoding="utf-8",
        )
        canvas = media.build_canvas_spec(project, slides, media.EXISTING_PPT_DECK_MODE)
        font_file = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        if not font_file.is_file():
            self.skipTest("approved subtitle font is unavailable")
        original_edge_version = media.edge_version
        media.edge_version = lambda: "test"
        self.addCleanup(setattr, media, "edge_version", original_edge_version)
        manifest = media.compose_video(
            project,
            slides,
            ffmpeg=ffmpeg,
            policy_id="TEST-001",
            title="外置字幕栏测试",
            voice="test",
            rate="0%",
            font="Heiti SC",
            font_file=font_file,
            canvas=canvas,
            existing_ppt_preflight={
                "pptx_page_ratio": 4 / 3,
                "rendered_page_ratio": 4 / 3,
                "dynamic_content": {
                    "transitions": [],
                    "animations": [],
                    "embedded_video": [],
                    "embedded_audio": [],
                },
                "dynamic_content_disposition": "none_detected",
            },
            measure_text=media.make_text_measurer(font_file, canvas.font_size),
            preferred_max_seconds=7.0,
            tail_seconds=0.55,
            output_tag="SMOKE",
            resume=False,
            online_authorized=True,
            subtitle_timing_source="test",
        )
        video = project / str(manifest["video"])
        probe = qa.run([str(ffmpeg), "-hide_banner", "-i", str(video)]).stderr
        self.assertEqual(qa.probe_dimensions(probe), (1440, 1170))
        frame = project / "qa/existing_ppt_smoke.png"
        media.run(
            [
                str(ffmpeg), "-y", "-ss", "0.2", "-i", str(video),
                "-frames:v", "1", "-update", "1", str(frame),
            ]
        )
        with Image.open(frame) as loaded:
            rendered = loaded.convert("RGB")
            slide_pixel = rendered.getpixel((10, 1078))
            band_pixel = rendered.getpixel((10, 1082))
        self.assertLess(sum(abs(actual - expected) for actual, expected in zip(slide_pixel, (12, 34, 56))), 18)
        self.assertLess(sum(abs(actual - expected) for actual, expected in zip(band_pixel, (17, 24, 39))), 18)
        with mock.patch.object(
            sys,
            "argv",
            ["qa_media.py", str(project), "--ffmpeg", str(ffmpeg), "--output-tag", "SMOKE"],
        ):
            self.assertEqual(qa.main(), 0)


if __name__ == "__main__":
    unittest.main()
