---
name: training-video-production
description: Create controlled, traceable Chinese policy training videos from DOCX, PDF, PPT, or PPTX source files, including source verification, evidence ledgers, rule coverage matrices, staged image-based PPT production with codex-ppt, per-slide narration, TTS, subtitles, FFmpeg assembly, QA, partial regeneration, and human release gates. Use when the user asks for 制度培训视频、条线培训视频、制度PPT和讲稿、逐页配音字幕、制度更新后局部重制，或要求调用“培训视频制作”技能。
---

# 培训视频制作

## 目标

把一项企业制度制作成一部基层员工也能听懂、所有制度性表述均可追溯、可审批且可局部重制的培训视频。制度正文始终是最高事实来源；不得为了表达顺畅补造规则。

## 开始前

1. 读取当前项目的 `AGENTS.md` 和根目录 `制作规范.md`（如存在）。项目专用规范优先于本技能的通用规范。
2. 完整读取 [production-standard.md](references/production-standard.md)。
3. 只在进入视觉阶段时读取 [visual-system.md](references/visual-system.md)。
4. 只在进入配音、字幕或视频阶段时读取 [technical-workflow.md](references/technical-workflow.md)。
5. 每次只处理一项制度；多项制度拆成多个项目、多个视频。

## 不得越过的原则

- 制度正文是最高事实来源；参考文件只能帮助理解，不得覆盖制度正文。
- 先核验源文件、建立制度证据台账和规则覆盖矩阵，再制作分页大纲。
- 默认制作规则级培训：讲清适用对象、职责、时限、金额、频次、审批、禁止项和后果；不扩展系统点击路径、表单字段、供应商名单等动作级 SOP。
- 编号、版本、金额、时限、频次、审批角色、禁止项、责任后果和附件关系属于高风险字段，必须保留原意并机械校验。
- 发现版本、编号、金额、时限、审批角色或附件冲突时立即暂停，请用户或制度负责人确认。
- 自动化不得取消制度负责人内容审批、成片审批和最终发布门禁。
- 不把当前试验制度的页数、规则数量、文件名或具体条款写死；页数服从学习效果。

## 标准流程

开始阶段先锁定课件模式：

- `native_generation`：未提供既有成品PPT，或要求重新设计课件；执行完整的大纲、视觉、图片后端、样稿和全套生成门禁。
- `existing_finished_ppt`：用户明确指定现成PPT/PPTX直接转视频；保留原页面比例、内容和视觉，以一次“既有PPT直接使用确认”替代重新设计课件的门禁。PPT格式源材料或视觉参考不得自动进入本模式。

### 阶段 1：登记与核验源文件

1. 区分正式制度正文、附件、参考方案和动态项目文件。
2. 记录文件名、角色、页数、版本、编号、有效状态、SHA-256 和处理时间。
3. 以 Docling 为主要解析器；轻量文档可用 MarkItDown，必要时用版面渲染补充人工核验。
4. 保留稳定来源定位：页码、条款、段落索引和原文片段。
5. 若项目尚未初始化，运行：

```bash
python scripts/init_policy_project.py PROJECT_DIR --policy-id POLICY_ID --title POLICY_TITLE --line 管家
```

完成标准：`manifest.json` 已登记正式来源，正式制度版本与有效状态无歧义。

### 阶段 2：锁定制度事实

1. 建立 `evidence_ledger.json`，每条规则至少包含：规则编号、来源页码、条款、段落索引、原文、规范化规则、风险词和课件处理方式。
2. 对金额、日期、时限、频次、审批角色、禁止项和责任后果进行逐字校验。
3. 运行事实校验：

```bash
python scripts/validate_policy_project.py PROJECT_DIR --stage facts
```

完成标准：全部制度规则均可回到原文；没有未解释的冲突或遗漏。

### 阶段 3：确认分页大纲

1. 按学习目标和认知负荷设计页数，不预设固定页数。
2. 每页只承载一个核心问题；基层员工先听懂“我需要知道什么、什么时候做、谁负责、什么不能错”。
3. 建立 `outline_rule_map.json`，为每条纳入培训的规则指定唯一主页面；总结页只复盘，不新增制度规则。
4. 生成覆盖矩阵并校验：

```bash
python scripts/render_coverage_matrix.py PROJECT_DIR
python scripts/validate_policy_project.py PROJECT_DIR --stage outline
```

5. 把分页大纲、课件外信息和覆盖结论交用户确认。未经明确确认，不生成正式灯片。
6. `existing_finished_ppt`模式仍建立规则覆盖和逐页来源映射，页序默认继承既有PPT；发现缺失、重复、错序或无来源陈述时暂停确认。

### 阶段 4：确认视觉方向

1. 使用“集团统一母体系 + 条线固定子识别”。
2. 继承已确认的管家、工程、秩序、绿化、保洁视觉编码；其他条线先给 2–3 个方向确认。
3. 先保证信息层级和可读性，再增加视觉表现。
4. `native_generation`模式全页背景保持16:9满幅；底部预留90px字幕安全区，安全区内不放正文、图表、页码或关键图形。
5. `existing_finished_ppt`模式不重新选择视觉方向，改为确认保留现有视觉、页面比例和页序。

完成标准：用户明确确认本制度的视觉方向。

### 阶段 5：确认图片后端

说明将使用的图片生成或渲染后端、是否会向外部服务发送内部内容及文本范围。未经确认，不把制度正文发送给外部图片服务。

完成标准：图片后端和外发边界获得确认。

`existing_finished_ppt`模式不调用图片后端，本阶段记为“不适用”；一旦需要重做或新增页面，退出直接使用模式并恢复相应门禁。

### 阶段 6：确认单页样稿

1. 选择信息密度中高、包含关键数字或流程关系的代表页。
2. `native_generation`模式生成一页16:9、带字幕安全区的样稿。
3. 同时检查视觉风格、数字准确性、基层可读性和底部安全区。

`existing_finished_ppt`模式以“既有PPT直接使用确认”和实际页面渲染检查替代生成样稿；记录PPTX路径、SHA-256、页数、页面宽高比、字体/缺字检查、动画、切换、嵌入视频和音频检测结果。发现动态内容时暂停，由用户批准静态化或改走保留动态效果的专用路径；旧式`.ppt`先转换为`.pptx`并复核。

完成标准：用户明确确认样稿；未经确认不生成全套。

### 阶段 7：生成并审批全套 PPT 与逐页讲稿

1. `native_generation`模式调用`$codex-ppt`，遵守其图片式PPT工作流；逐页生成，保存逐页提示词、原图和运行状态。`existing_finished_ppt`模式直接渲染已确认PPT，不重新设计或改动页面比例。
2. 每页讲稿只解释本页已映射规则，不引入新制度要求；口语化但不改写规则含义。
3. 将制度原文、高风险数字、灯片文字和讲稿进行交叉核验。
4. 输出 PPTX、`speech.md`、逐页来源映射和 QA 记录。
5. 运行：

```bash
python scripts/validate_policy_project.py PROJECT_DIR --stage deck
```

完成标准：`native_generation`模式由用户明确批准生成的全套灯片与讲稿；`existing_finished_ppt`模式不重新审批视觉方案，但仍须批准既有整套页面与讲稿的内容对应关系。进入视频阶段的授权不等于允许外发完整讲稿。

### 阶段 8：逐页配音、字幕和视频合成

1. TTS 前记录所选服务商、拟处理文本范围及本地或在线运行方式；只有在线分支需要说明信息外发风险。
2. 在线 TTS 必须取得针对当前材料的明确外发授权；此前样音或其他制度的授权不可复用。
3. 默认V4成片链路固定为：MeloTTS官方中文固定音色`ZH`（CPU、速度0.95）→ 温和压缩与两遍整体响度校准 → 同一最终WAV的MFA普通话强制对齐 → 语义优先SRT/ASS → FFmpeg逐页片段及完整视频 → 全量媒体QA。模型只允许从本地缓存加载，不启用真人声音克隆，不允许失败后回退在线服务、字符估时或其他合成器。
4. 本地生成前必须先列出或生成官方音色试听，记录音色确认依据；自动术语门禁发现英文字母、制度编号或阿拉伯数字即停止，须先批准自然中文讲稿改写。生僻词仍须人工试听。MFA只提供同一最终WAV的真实音频时间，字幕文本必须与批准讲稿逐字一致。
5. MeloTTS＋MFA基准五页试验已通过完整技术QA及人工自然度、语速、音量和跨页一致性复听，因此设为正式默认；每个项目仍须在`manifest.json`记录本地官方音色及确认依据。此默认不等于允许自动改写讲稿或覆盖既有正式媒体。
6. Edge TTS保留为显式在线备选，仅在用户为当前材料单独批准外发并主动选择`--provider edge`时使用；它使用同一音频流的`WordBoundary`事件生成逐页SRT。不得由本地链路静默回退至Edge。
7. `native_generation`模式使用1920×1080画布、44px单行字幕和页面内底部90px字幕带；`existing_finished_ppt`模式完整保留PPT渲染画面，在页面画布之外向下增加高度为页面高度8.33%的全宽字幕栏。字幕栏高度、字号、描边和垂直边距按页面高度缩放，字幕目标宽度、硬上限和水平边距按视频宽度缩放；例如1440×1080的4:3页面使用90px字幕栏、44px字幕、1125px常规目标和1200px硬上限，成片为1440×1170而不是16:9。两种模式均使用30fps、H.264 + AAC；不得裁切、拉伸或缩小既有PPT来适配固定画布。优先保持完整句子，超宽时依次按强停顿、逗号、自然语义连接词和词语边界拆分；不得孤立标点，“先、再、但、并”等连接词不得悬在字幕末尾，不得拆开制度名称、编号、金额、日期、百分比、计量单位或岗位名称。合成失败必须停止，不得自动降级工具。
8. 默认本地链路按顺序运行（运行路径按本机环境填写）：

```bash
python scripts/media_pipeline.py PROJECT_DIR --list-local-voices
python scripts/media_pipeline.py PROJECT_DIR --melo-python /path/to/melo/python --preview-local-voice --output-tag V4
python scripts/media_pipeline.py PROJECT_DIR --melo-python /path/to/melo/python --mfa /path/to/mfa --mfa-root /path/to/mfa-root --pkuseg-home /path/to/pkuseg --confirm-local-voice ZH --voice-confirmation-basis '已批准的试听记录' --resume --output-tag V4
python scripts/qa_media.py PROJECT_DIR --output-tag V4
python scripts/validate_policy_project.py PROJECT_DIR --stage media --output-tag V4
```

`existing_finished_ppt`模式必须先在`manifest.json`记录`deck_input.mode`、PPTX路径、直接使用批准和动态内容处置，再运行同一命令；媒体脚本会从实际灯片图像读取尺寸并生成外置字幕栏。

Edge在线备选的默认音色为`zh-CN-XiaoxiaoNeural`、语速`-5%`。仅在当前材料外发获授权后运行：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --authorize-online-tts --resume --output-tag V4
python scripts/qa_media.py PROJECT_DIR --output-tag V4
```

9. 旧Edge V2/V3项目仅重做画面或字幕时，显式选择Edge并复用已批准音频：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --skip-tts --reflow-existing-subtitles --reuse-provenance-tag V2 --resume --output-tag V4
```

10. 正式媒体QA必须执行完整模式，不得以`--fast`结果作为交付依据；必须覆盖实际全部页面的画面对照、页面比例、页面与字幕栏边界、动态分辨率、字幕实际像素宽度、语义断句、孤立标点、受保护字段、讲稿逐字一致、词级时间轴、完整解码、响度、真峰值和超过2秒异常静音。单条字幕优先显示1.2—6秒，超过约7秒继续按语义拆分；过短或停留不舒适的片段列入人工复核。本地链路还须核对术语门禁、音色确认、本地运行、禁止网络回退及逐页MFA对齐文件哈希。

完成标准：完整媒体技术QA通过，字幕无重叠或溢出、音画顺序正确、配音与字幕来自同一词级时间轴、无超过2秒异常静音；既有成品PPT还须确认原页面比例和内容完整、外置字幕栏不覆盖页面。

### 阶段 9：最终审批与发布

1. 由制度负责人核验内容准确性。
2. 由基层员工代表试看，确认能听懂关键规则。
3. 记录审批人、日期、版本和结果；审批前状态只能是“待发布”。
4. 交付视频、PPTX、讲稿、全程字幕、证据台账、规则覆盖矩阵、来源映射、QA 报告和审批记录。

完成标准：制度负责人和发布责任人明确批准后才标记“已发布”。

## 需求尚未确认时的提问方式

一次提出 6–10 个关键问题，并给出推荐答案，集中确认：正式来源、制度编号与版本、培训目标、受众、规则级边界、附件口径、预计使用场景、视觉条线、在线服务边界和审批人。先形成“共同理解确认稿”，再开始正式制作。

## 变更与局部重制

1. 对比新旧文件哈希、条款和证据台账。
2. 找出变更规则编号。
3. 通过覆盖矩阵定位受影响页面、讲稿、音频、字幕和视频片段。
4. 只重制受影响页面及其后续媒体，未受影响页面沿用已批准版本。
5. 重新执行相关门禁和 QA；制度变更仍需制度负责人审批。

## 最终汇报

先报告完成状态，再提供可点击的绝对路径。至少说明：制度版本、灯片页数、视频时长、TTS 服务、字幕规格、事实/覆盖/媒体 QA 结果、仍待谁审批。不要把技术生成成功描述成业务发布成功。
