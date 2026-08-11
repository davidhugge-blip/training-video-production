# 技术工作流与执行约束

## 1. 技术主链路

```text
制度源文件
  → Docling 结构化解析
  → MarkItDown 轻量备选／版面渲染复核
  → 制度证据台账
  → 规则覆盖矩阵
  → codex-ppt 图片式 PPT 与逐页讲稿
  → PPT Master 逐页讲稿、逐页配音、逐页字幕机制
  → Edge TTS 试制或本地 CosyVoice 生产
  → FFmpeg 逐页片段与完整视频
  → 事实、视觉、媒体、人审四层 QA
```

PPT Master 的自由内容生成逻辑不得替代制度证据台账；只借鉴规格锁定、逐页讲稿、逐页配音、运行状态和局部重制机制。

## 2. 依赖策略

- 文档：优先 Docling；MarkItDown 用于轻量快速解析；DOCX/PDF 渲染用于核对页码、表格和视觉结构。
- PPT：调用已安装的 `$codex-ppt`，遵守大纲、视觉、图片后端、样稿、全套授权门禁。
- 配音：试制可用 `edge-tts`；生产优先评估 CosyVoice 本地部署。
- 视频：FFmpeg 为合成和 QA 核心。
- Python：建议 3.11 或 3.12；媒体环境需包含 Pillow 和 edge-tts（使用 Edge 时）。

## 3. 项目初始化

```bash
python scripts/init_policy_project.py /path/to/POLICY_ID \
  --policy-id QXXX-XXX-001 \
  --title '制度名称' \
  --line 管家
```

初始化脚本创建目录、空证据台账、空映射表和审批记录，不会生成任何制度结论。

## 4. 事实与覆盖校验

填完证据台账后：

```bash
python scripts/validate_policy_project.py /path/to/POLICY_ID --stage facts
```

填完分页映射后：

```bash
python scripts/render_coverage_matrix.py /path/to/POLICY_ID
python scripts/validate_policy_project.py /path/to/POLICY_ID --stage outline
```

校验失败必须先修复来源或映射，不得通过删除高风险词、降低规则强度或跳过规则来“通过”。

## 5. 讲稿格式

媒体脚本读取 `speech.md`，格式必须稳定：

```markdown
# 逐页讲稿

## Slide 1: 封面

这里是第一页批准后的完整讲稿。

---

## Slide 2: 本制度解决什么问题

这里是第二页批准后的完整讲稿。
```

页码必须从 1 连续编号。每页对应 `origin_image/slide_01.png`、`slide_02.png` 等。

## 6. 在线 TTS 授权

调用 Edge TTS 前，向用户明确说明：

1. 服务商：Microsoft Edge 在线语音服务。
2. 发送范围：当前制度已批准的全部逐页讲稿，或明确的试音片段。
3. 风险：文本将离开本地环境并发送给第三方在线服务。
4. 备选：不授权时使用本地离线 TTS。

只有用户对当前材料明确同意后，才能在命令中加入 `--authorize-online-tts`。进入视频制作阶段、此前样音授权或其他制度授权均不可替代本次授权。

## 7. 媒体生成

默认 Edge 试制：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID \
  --provider edge \
  --voice zh-CN-XiaoxiaoNeural \
  --rate=-5% \
  --authorize-online-tts
```

只检查输入和环境，不生成媒体：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID --check-only
```

复用现有逐页音频和 SRT，重做字幕或画面：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID --skip-tts
```

脚本默认规格：

- `1920×1080`、30fps、H.264、AAC。
- 字幕带 `y=990`、高 90px。
- 字幕 `Heiti SC`、44px、单行、最多 24 字符。
- Edge 默认声音 `zh-CN-XiaoxiaoNeural`、语速 `-5%`。
- 音频目标 -16 LUFS，片尾呼吸时间 0.55 秒。

## 8. PPT Master 兼容

媒体脚本会按以下顺序查找 `notes_to_audio.py`：

1. `--notes-to-audio` 显式路径。
2. 项目根目录 `third_party/ppt-master/skills/ppt-master/scripts/notes_to_audio.py`。
3. 环境变量 `PPT_MASTER_NOTES_TO_AUDIO`。

找不到时，先安装或克隆 PPT Master，再显式传入脚本路径。不要下载未知来源的同名脚本。

## 9. FFmpeg 查找顺序

1. `--ffmpeg` 显式路径。
2. 当前环境中的 `ffmpeg`。
3. `imageio-ffmpeg` 捆绑二进制。

不得长期依赖 PowerPoint 桌面端批量导出视频。

## 10. 媒体 QA

```bash
python scripts/qa_media.py /path/to/POLICY_ID
```

检查项：

- 视频完整解码。
- 1920×1080、30fps、H.264、AAC 48kHz。
- 逐页音频、逐页字幕、ASS 和灯片数量相等。
- 全程字幕与 `speech.md` 逐字一致。
- 源字幕不超过 20 字符，显示字幕不超过 24 字符。
- ASS 使用 44px 字幕。
- 无超过 2 秒异常静音。
- 每页视频顶部 990px 与对应灯片画面一致。

最终人工检查仍必须覆盖：术语读音、停顿、自然度、字幕是否舒适和基层员工是否听懂。

## 11. 局部重制

制度更新时先做规则级差异：

```text
新旧制度 → 变更条款 → 变更 rule_id → 受影响 slide → 受影响 speech
       → 重制 slide/audio/srt/clip → 拼接成新版本 → 重新 QA 和审批
```

未受影响页面可以复用，但必须记录来源版本和复用范围。
