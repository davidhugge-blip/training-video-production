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

### 阶段 4：确认视觉方向

1. 使用“集团统一母体系 + 条线固定子识别”。
2. 继承已确认的管家、工程、秩序、绿化、保洁视觉编码；其他条线先给 2–3 个方向确认。
3. 先保证信息层级和可读性，再增加视觉表现。
4. 全页背景保持 16:9 满幅；底部预留 90px 字幕安全区，安全区内不放正文、图表、页码或关键图形。

完成标准：用户明确确认本制度的视觉方向。

### 阶段 5：确认图片后端

说明将使用的图片生成或渲染后端、是否会向外部服务发送内部内容及文本范围。未经确认，不把制度正文发送给外部图片服务。

完成标准：图片后端和外发边界获得确认。

### 阶段 6：确认单页样稿

1. 选择信息密度中高、包含关键数字或流程关系的代表页。
2. 生成一页 16:9、带字幕安全区的样稿。
3. 同时检查视觉风格、数字准确性、基层可读性和底部安全区。

完成标准：用户明确确认样稿；未经确认不生成全套。

### 阶段 7：生成并审批全套 PPT 与逐页讲稿

1. 调用 `$codex-ppt`，遵守其图片式 PPT 工作流；逐页生成，保存逐页提示词、原图和运行状态。
2. 每页讲稿只解释本页已映射规则，不引入新制度要求；口语化但不改写规则含义。
3. 将制度原文、高风险数字、灯片文字和讲稿进行交叉核验。
4. 输出 PPTX、`speech.md`、逐页来源映射和 QA 记录。
5. 运行：

```bash
python scripts/validate_policy_project.py PROJECT_DIR --stage deck
```

完成标准：用户明确批准全套灯片与讲稿。进入视频阶段的授权不等于允许外发完整讲稿。

### 阶段 8：逐页配音、字幕和视频合成

1. TTS 前单独说明服务商、拟发送文本范围和信息外发风险。
2. 在线 TTS 必须取得针对当前材料的明确外发授权；此前样音或其他制度的授权不可复用。没有授权时使用本地离线方案。
3. 试制可使用 Edge TTS；生产优先评估本地 CosyVoice。F5-TTS 常见中英模型的非商用许可不进入企业生产主链路。
4. 使用逐页讲稿生成逐页音频和逐页 SRT；字幕必须与讲稿逐字一致。
5. 使用 FFmpeg 合成 1920×1080、30fps、H.264 + AAC 视频。字幕 44px、单行优先、置于底部 90px 字幕带内。
6. 在线 Edge TTS 获授权后运行：

```bash
python scripts/media_pipeline.py PROJECT_DIR --provider edge --authorize-online-tts
python scripts/qa_media.py PROJECT_DIR
```

7. 仅重做画面或字幕时复用已批准音频：

```bash
python scripts/media_pipeline.py PROJECT_DIR --skip-tts
```

完成标准：媒体技术 QA 通过，字幕不遮挡正文，音画顺序正确，无超过 2 秒异常静音。

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
