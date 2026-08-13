# 培训视频制作

一个面向中国企业制度文件的 Codex Skill，用于把 DOCX、PDF、PPT 或 PPTX 制作成可追溯、可审批、可局部重制的培训 PPT、逐页讲稿、配音字幕和完整视频。

## 核心能力

- 一项制度对应一部视频，便于制度更新后单独重制。
- 制度正文是最高事实来源，先建立制度证据台账和规则覆盖矩阵。
- 按大纲、视觉方向、图片后端、单页样稿、全套 PPT、讲稿、配音和发布逐级审批。
- 支持管家、工程、秩序、绿化、保洁五条线固定视觉编码。
- 使用 16:9 满幅灯片、90px 字幕安全区、44px单行字幕和每条不超过20个显示字符的标准。
- V2成片链路固定为 Edge TTS 同源词级时间轴、逐页MP3/SRT、ASS字幕和FFmpeg合成；失败时停止，不静默切换本地临时工具。
- 完整媒体QA覆盖讲稿逐字一致、词级时间轴、字幕实际像素宽度、全部页面顺序与主画面对照、完整解码、响度、真峰值和异常静音。
- 在线 TTS 必须针对当前材料单独取得外发授权。
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

在线配音获得当前材料专项授权后，V2媒体阶段使用：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --authorize-online-tts --resume --output-tag V2
python scripts/qa_media.py PROJECT_DIR --output-tag V2
python scripts/validate_policy_project.py PROJECT_DIR --stage media
```

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
