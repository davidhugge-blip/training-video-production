# 技术工作流与执行约束

## 1. 技术主链路

```text
制度源文件
  → Docling 结构化解析
  → MarkItDown 轻量备选／版面渲染复核
  → 制度证据台账
  → 规则覆盖矩阵
  → codex-ppt 图片式 PPT 与逐页讲稿
  → 默认MeloTTS本地WAV＋MFA强制对齐；经当前材料授权可显式选择Edge WordBoundary在线备选
  → FFmpeg 逐页片段与完整视频
  → 事实、视觉、媒体、人审四层 QA
```

PPT Master 的自由内容生成逻辑不得替代制度证据台账。媒体生产脚本不得依赖项目外 PPT Master；本技能只借鉴其逐页组织和断点重制思路，并在技能内确定性实现。

## 2. 依赖策略

- 文档：优先 Docling；MarkItDown 用于轻量快速解析；DOCX/PDF 渲染用于核对页码、表格和视觉结构。
- PPT：调用已安装的 `$codex-ppt`，遵守大纲、视觉、图片后端、样稿、全套授权门禁。
- 配音：正式默认为已通过五页技术QA及人工复听的MeloTTS官方`ZH`固定音色＋MFA普通话强制对齐；`edge-tts`词级事件链路仅为显式在线备选。
- 视频：FFmpeg 为合成和 QA 核心。
- Python：主媒体环境建议3.11或3.12并包含Pillow；MeloTTS与MFA使用彼此隔离的项目内环境；只有显式Edge备选环境需要edge-tts。

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

### 6.1 默认本地MeloTTS与MFA门禁

本地链路不发送讲稿到在线服务，也不允许网络回退。执行前必须：

1. 列出或生成MeloTTS官方中文固定音色试听，记录`ZH`音色确认依据；不启用真人声音克隆。
2. 运行术语门禁。出现英文字母、制度编号或阿拉伯数字时停止；先在`speech.md`中完成经内容负责人批准的自然中文改写。生僻词由人工试听确认。
3. 使用速度0.95生成逐页原始WAV，采用温和压缩和两遍整体响度校准，最终保存48kHz、单声道、16位WAV。
4. 对最终WAV运行MFA普通话模型，逐页保存TextGrid和对齐JSON；字幕文本仍逐字来自批准的`speech.md`，MFA只提供真实音频时间。
5. 基线五页试验已通过完整媒体QA及人工自然度、语速、音量和跨页一致性复听，正式默认据此生效。每个正式项目仍须在`manifest.json`记录音色确认并通过完整媒体QA和最终人工试看。

## 7. 媒体生成

媒体脚本从`manifest.json`读取课件模式：

```json
{
  "deck_input": {
    "mode": "existing_finished_ppt",
    "path": "既有成品课件.pptx",
    "sha256": "已核验的文件哈希",
    "dynamic_content_disposition": "none_detected"
  },
  "approvals": {
    "existing_ppt_direct_use": "approved"
  }
}
```

`native_generation`为默认值并保持原16:9链路。`existing_finished_ppt`只有在PPTX路径、SHA-256、直接使用批准和动态内容处置均已记录时才能执行；旧式`.ppt`先转换为`.pptx`并复核。`--check-only`会核对PPTX页面比例、实际灯片图像尺寸以及动画、切换、嵌入视频和音频，发现未处置动态内容即停止。

默认MeloTTS＋MFA本地制作：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID --list-local-voices

python scripts/media_pipeline.py /path/to/POLICY_ID \
  --melo-python /path/to/melo/python \
  --preview-local-voice \
  --melo-speaker ZH \
  --melo-speed 0.95 \
  --output-tag V4

python scripts/media_pipeline.py /path/to/POLICY_ID \
  --melo-python /path/to/melo/python \
  --mfa /path/to/mfa \
  --mfa-root /path/to/mfa-root \
  --pkuseg-home /path/to/pkuseg \
  --melo-speaker ZH \
  --melo-speed 0.95 \
  --confirm-local-voice ZH \
  --voice-confirmation-basis '已批准的试听记录' \
  --resume \
  --output-tag V4

python scripts/qa_media.py /path/to/POLICY_ID --output-tag V4
python scripts/validate_policy_project.py /path/to/POLICY_ID --stage media --output-tag V4
```

Edge在线备选仅在当前材料获得专项外发授权后运行：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID \
  --provider edge \
  --voice zh-CN-XiaoxiaoNeural \
  --rate=-5% \
  --authorize-online-tts \
  --resume \
  --output-tag V4
```

只检查输入和环境，不生成媒体：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID --melo-python /path/to/melo/python --mfa /path/to/mfa --mfa-root /path/to/mfa-root --pkuseg-home /path/to/pkuseg --check-only
```

复用现有逐页音频和 SRT，只重做画面：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID --melo-python /path/to/melo/python --mfa /path/to/mfa --mfa-root /path/to/mfa-root --pkuseg-home /path/to/pkuseg --skip-tts --resume --output-tag V4
```

复用已批准的V2音频并按V4语义规则重排字幕：

```bash
python scripts/media_pipeline.py /path/to/POLICY_ID \
  --provider edge \
  --skip-tts \
  --reflow-existing-subtitles \
  --reuse-provenance-tag V2 \
  --resume \
  --output-tag V4
```

该操作不得调用TTS。V4本地新生成项目保存逐页MFA对齐文件和哈希；Edge备选保存逐页WordBoundary事件。旧项目没有原始词级事件时，只能合并既有SRT时间段，不按字符比例重算。

脚本默认规格：

- `native_generation`：`1920×1080`、字幕带`y=990`且高90px、`Heiti SC` 44px单行字幕。
- `existing_finished_ppt`：保留灯片图像的实际宽高比和完整画面，在图像底边之外增加高度约为灯片高度8.33%的全宽字幕栏；输出高度为灯片高度加字幕栏高度，宽高按H.264所需偶数像素补齐，不强制16:9。字幕栏高度、字号、描边和垂直边距按灯片高度缩放；目标宽度、硬上限和水平边距按视频宽度缩放，不接受手工几何覆盖参数。
- 两种模式均为30fps、H.264、AAC；1920×1080页面基准下，常规目标宽度1500px，受保护内容硬上限1600px，QA必须按真实字体测量像素宽度。
- 字幕优先保持完整句子；超宽时依次在分号或冒号、逗号、自然语义连接词、词语边界处分段。禁止按固定字符数机械截断。
- 能容纳的书名号、引号和括号必须成对显示；超长内容跨字幕时标点必须依附对应文字，不得单独显示。
- 制度名称、编号、金额、日期、百分比、计量单位和岗位名称属于受保护内容，不得从内部拆开。
- 单条优先显示1.2—6秒；超过约7秒继续按语义拆分，过短片段在宽度和语义允许时合并。
- Melo本地默认固定官方声音`ZH`、速度0.95；最终WAV为48kHz单声道16位，MFA对齐文件和术语门禁报告必须保留哈希。
- Edge在线备选默认声音`zh-CN-XiaoxiaoNeural`、语速`-5%`。
- 音频目标 -16 LUFS，片尾呼吸时间 0.55 秒。

## 8. 工具锁定与失败策略

- MeloTTS、MFA及Edge备选均由技能自带`media_pipeline.py`编排，不调用项目外`notes_to_audio.py`。
- MeloTTS只从本地缓存加载；MFA必须对最终WAV执行真实音频强制对齐。任一模型、术语、音色确认或对齐步骤失败都停止，不得改用字符估时或在线TTS。
- 视频只允许FFmpeg合成；禁止自动切换Swift/AVFoundation、PowerPoint或其他视频后端。
- 在线服务、依赖、字体或FFmpeg不可用时直接停止；排除问题后用`--resume`续跑。
- 单页Edge请求默认180秒超时，超时后最多重试3次；已成功页面从断点状态复用。
- 不允许在失败时自动生成AIFF，不允许用整页字符比例估算字幕时间。

## 9. FFmpeg 查找顺序

1. `--ffmpeg` 显式路径。
2. 当前环境中的 `ffmpeg`。
3. `imageio-ffmpeg` 捆绑二进制。

不得长期依赖 PowerPoint 桌面端批量导出视频。

## 10. 媒体 QA

```bash
python scripts/qa_media.py /path/to/POLICY_ID --output-tag V4
python scripts/validate_policy_project.py /path/to/POLICY_ID --stage media --output-tag V4
```

检查项：

- 视频完整解码。
- 实际分辨率与媒体清单一致、宽高为编码兼容的偶数像素、30fps、H.264、AAC 48kHz；既有成品PPT不要求16:9。
- 逐页音频、逐页字幕、ASS 和灯片数量相等。
- 全程字幕与 `speech.md` 逐字一致。
- 每条字幕使用真实字体文件测量单行像素宽度；原生1920px宽画布的常规目标为1500px、硬上限为1600px，既有成品PPT按实际视频宽度同比例缩放。
- 不得出现孤立标点、闭合标点位于字幕开头、开放标点或“先、再、但、并”等连接词悬在字幕末尾、受保护内容被拆开的情况。
- 原生1080px高页面的ASS使用44px字幕；既有成品PPT按实际页面高度同比例缩放，并与媒体清单记录一致。
- 无超过 2 秒异常静音。
- 原生制作PPT的每页视频顶部990px与对应灯片一致；既有成品PPT的全部原页面区域与对应灯片一致，外置字幕栏从页面底边开始且不覆盖页面。
- 既有成品PPT的PPTX页面比例、渲染页面比例、动态内容检测和处置记录一致。
- 本地新生成项目的逐页字幕时间来自同一最终WAV的MFA强制对齐并保留对齐文件与哈希；Edge备选来自同一音频流的WordBoundary事件。旧项目字幕返修只能复用既有SRT时间段和已批准音频。
- 逐页音频、字幕、ASS、视频片段的哈希与媒体清单一致。

最终人工检查仍必须覆盖：术语读音、停顿、自然度、语义断句、标点配对、字幕是否舒适和基层员工是否听懂。

## 11. 局部重制

制度更新时先做规则级差异：

```text
新旧制度 → 变更条款 → 变更 rule_id → 受影响 slide → 受影响 speech
       → 重制 slide/audio/srt/clip → 拼接成新版本 → 重新 QA 和审批
```

未受影响页面可以复用，但必须记录来源版本和复用范围。
