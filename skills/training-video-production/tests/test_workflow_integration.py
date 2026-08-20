from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import init_policy_project as initializer  # noqa: E402
import media_pipeline as media  # noqa: E402
import validate_policy_project as validator  # noqa: E402


class WorkflowIntegrationTests(unittest.TestCase):
    def test_list_local_voices_needs_no_project_or_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_project = Path(directory) / "not-created"
            output = io.StringIO()
            with mock.patch.object(
                sys,
                "argv",
                ["media_pipeline.py", str(missing_project), "--list-local-voices"],
            ), redirect_stdout(output):
                self.assertEqual(media.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["official_fixed_voices"], ["ZH"])
        self.assertFalse(payload["voice_cloning"])

    def test_initializer_records_local_voice_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "policy"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "init_policy_project.py",
                    str(project),
                    "--policy-id",
                    "ABC-RULE-001",
                    "--title",
                    "示例制度",
                    "--line",
                    "其他",
                ],
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(initializer.main(), 0)
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            approval = (project / "approvals/审批记录.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["voiceover"]["default_provider"], "melo")
        self.assertEqual(manifest["voiceover"]["output_tag"], "V4")
        self.assertIsNone(manifest["voiceover"]["selected_voice"])
        self.assertEqual(manifest["approvals"]["local_voice_selection"], "pending")
        self.assertIn("本地官方音色确认", approval)

    def test_local_voice_confirmation_is_written_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            manifest = {"approvals": {}, "updated_at": "old"}
            (project / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            media.record_local_voice_selection(
                project,
                manifest,
                speaker="ZH",
                speed=0.95,
                confirmation_basis="已批准的官方音色试听记录",
            )
            stored = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["approvals"]["local_voice_selection"], "approved")
        self.assertEqual(stored["voiceover"]["selected_provider"], "melo")
        self.assertEqual(stored["voiceover"]["selected_voice"], "ZH")
        self.assertFalse(stored["voiceover"]["voice_cloning"])
        self.assertFalse(stored["voiceover"]["online_fallback"])
        self.assertNotEqual(stored["updated_at"], "old")

    def test_v4_wav_media_gate_uses_explicit_tag_and_local_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for name in ("audio", "subtitles", "video", "qa"):
                (project / name).mkdir()
            (project / "audio/slide_01.wav").write_bytes(b"wav")
            (project / "subtitles/slide_01.srt").write_text("subtitle", encoding="utf-8")
            (project / "video/final.mp4").write_bytes(b"video")
            (project / "manifest.json").write_text(
                json.dumps(
                    {
                        "approvals": {"local_voice_selection": "approved"},
                        "voiceover": {
                            "default_provider": "melo",
                            "selected_provider": "melo",
                            "selected_voice": "ZH",
                            "speed": 0.95,
                            "voice_confirmation_basis": "已批准的官方音色试听记录",
                            "voice_cloning": False,
                            "online_fallback": False,
                            "output_tag": "V4",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (project / "qa/media_manifest_V3.json").write_text(
                json.dumps({"pipeline_version": "3.1", "slide_count": 99}),
                encoding="utf-8",
            )
            (project / "qa/media_qa_V3.json").write_text(
                json.dumps({"status": "fail", "pipeline_version": "3.1"}),
                encoding="utf-8",
            )
            (project / "qa/media_manifest_V4.json").write_text(
                json.dumps(
                    {
                        "pipeline_version": "4.1",
                        "slide_count": 1,
                        "audio": {
                            "provider": "MeloTTS local ZH",
                            "speaker": "ZH",
                            "speed": 0.95,
                            "voice_confirmed": True,
                            "voice_confirmation_basis": "已批准的官方音色试听记录",
                            "voice_cloning": False,
                            "local_only": True,
                            "online_external_transfer_authorized": False,
                            "network_fallback": False,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (project / "qa/media_qa_V4.json").write_text(
                json.dumps({"status": "pass", "pipeline_version": "4.1"}),
                encoding="utf-8",
            )
            errors: list[str] = []
            validator.validate_media(project, 1, errors, output_tag="V4")
        self.assertEqual(errors, [])

    def test_v4_local_media_gate_rejects_missing_voice_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for name in ("audio", "subtitles", "video", "qa"):
                (project / name).mkdir()
            (project / "audio/slide_01.wav").write_bytes(b"wav")
            (project / "subtitles/slide_01.srt").write_text("subtitle", encoding="utf-8")
            (project / "video/final.mp4").write_bytes(b"video")
            (project / "manifest.json").write_text(
                json.dumps({"approvals": {}, "voiceover": {}}),
                encoding="utf-8",
            )
            (project / "qa/media_manifest_V4.json").write_text(
                json.dumps(
                    {
                        "pipeline_version": "4.1",
                        "slide_count": 1,
                        "audio": {
                            "provider": "MeloTTS local ZH",
                            "speaker": "ZH",
                            "speed": 0.95,
                            "voice_confirmed": True,
                            "voice_confirmation_basis": "记录",
                            "voice_cloning": False,
                            "local_only": True,
                            "online_external_transfer_authorized": False,
                            "network_fallback": False,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (project / "qa/media_qa_V4.json").write_text(
                json.dumps({"status": "pass", "pipeline_version": "4.1"}),
                encoding="utf-8",
            )
            errors: list[str] = []
            validator.validate_media(project, 1, errors, output_tag="V4")
        self.assertIn("local official voice selection is not approved", errors)


if __name__ == "__main__":
    unittest.main()
