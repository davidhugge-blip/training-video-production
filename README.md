# 培训视频制作

一个面向中国企业制度文件的 Codex Skill，用于把 DOCX、PDF、PPT 或 PPTX 制作成可追溯、可审批、可局部重制的培训 PPT、逐页讲稿、配音字幕和完整视频。

## 核心能力

- 一项制度对应一部视频，便于制度更新后单独重制。
- 制度正文是最高事实来源，先建立制度证据台账和规则覆盖矩阵。
- 按大纲、视觉方向、图片后端、单页样稿、全套 PPT、讲稿、配音和发布逐级审批。
- 支持管家、工程、秩序、绿化、保洁五条线固定视觉编码。
- 原生制作使用16:9满幅灯片、页面内90px字幕安全区和44px单行字幕；用户明确提供既有成品PPT直接转视频时，保留原页面比例并在页面外增加同比例字幕栏，成片不强制16:9。
- V4成片链路默认使用MeloTTS官方中文固定音色`ZH`本地生成、MFA同一最终WAV强制对齐、语义优先SRT/ASS和FFmpeg合成；失败时停止，不静默回退在线服务或字符估时。
- 完整媒体QA覆盖讲稿逐字一致、同源时间轴、语义断句、成对标点、连接词不悬空、受保护字段、字幕实际像素宽度、全部页面顺序与主画面对照、完整解码、响度、真峰值和异常静音。
- Edge词级时间轴链路仅为显式在线备选，必须针对当前材料单独取得外发授权。
- 通过规则编号定位受影响页面，支持局部重制。

## 安装

```bash
git clone https://github.com/davidhugge-blip/training-video-production.git
cp -R training-video-production/skills/training-video-production ~/.codex/skills/
```

重新打开 Codex 任务后即可调用。

## 调用

可以直接说：

> 使用培训视频制作技能，把这份制度制作成完整培训视频。

也可以显式调用：

```text
$training-video-production
```

V4默认本地媒体阶段先列出或试听官方音色，再在记录确认依据后生成：

```bash
python scripts/media_pipeline.py PROJECT_DIR --list-local-voices
python scripts/media_pipeline.py PROJECT_DIR --melo-python /path/to/melo/python --preview-local-voice --output-tag V4
python scripts/media_pipeline.py PROJECT_DIR --melo-python /path/to/melo/python --mfa /path/to/mfa --mfa-root /path/to/mfa-root --pkuseg-home /path/to/pkuseg --confirm-local-voice ZH --voice-confirmation-basis '已批准的试听记录' --resume --output-tag V4
python scripts/qa_media.py PROJECT_DIR --output-tag V4
python scripts/validate_policy_project.py PROJECT_DIR --stage media --output-tag V4
```

Edge在线备选只有在当前材料专项外发获授权后才能显式运行：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --authorize-online-tts --resume --output-tag V4
```

若使用既有成品PPT，先在`manifest.json`把`deck_input.mode`记录为`existing_finished_ppt`，登记PPTX路径与SHA-256、动态内容处置，并把`approvals.existing_ppt_direct_use`记为`approved`。随后先运行检查：

```bash
python scripts/media_pipeline.py PROJECT_DIR --melo-python /path/to/melo/python --mfa /path/to/mfa --mfa-root /path/to/mfa-root --pkuseg-home /path/to/pkuseg --check-only
```

检查会核对PPTX页面比例、渲染图尺寸以及动画、切换、嵌入视频和音频；未处置动态内容时停止，不会静默静态化。

若旧项目只需返修字幕，复用已批准的V2音频和SRT时间段：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --skip-tts --reflow-existing-subtitles --reuse-provenance-tag V2 --resume --output-tag V4
python scripts/qa_media.py PROJECT_DIR --output-tag V4
python scripts/validate_policy_project.py PROJECT_DIR --stage media --output-tag V4
```

该操作不会重新调用TTS。

正式交付不得用`qa_media.py --fast`替代完整媒体QA。

## 仓库结构

```text
skills/training-video-production/
├── SKILL.md
├── agents/openai.yaml
├── references/
├── scripts/
└── assets/
```

## 数据安全

本仓库只包含通用流程、模板、视觉规范和自动化脚本，不包含任何试验制度正文、内部讲稿、配音、字幕或培训成片。自动化不得绕过制度负责人内容审批、在线服务外发授权和最终发布门禁。
