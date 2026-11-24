---
name: pdf-layout-reader
description: 读取科研 PDF（单栏/双栏/多栏、扫描件、表格页），产出带页码与区块定位的结构化结果：正文阅读顺序、章节层级、表格 Markdown、图表题注、公式原字符保真、参考文献，以及可直接引用的证据片段。当任务涉及“解析/阅读这篇 PDF”“把论文转成结构化文本”“还原双栏阅读顺序”“提取表格和章节”“扫描版文献识别”“基于原文回答问题并给出页码依据”时使用。不负责论文科学结论生成、绘图美化或写作润色。
version: 1.0.0
---

# pdf-layout-reader

把一份科研 PDF 变成 **可回溯的结构化材料**：每一段正文、每一张表格都能指回 `(页码, 区块 bbox)`，读不出来的地方如实标注，不靠猜补齐。

## 何时使用

- 任务给了 PDF/文献，要求解析、翻译、问答、抽取、核验或知识结构化；
- 需要"读得对"：双栏不串栏、页眉页脚不入正文、跨栏跨页段落不断裂；
- 需要"拆得清"：章节层级、表格行列、公式上下标与希腊字母、参考文献条目；
- 需要"核得回"：答案必须带页码/区块证据。

不适用：生成论文结论、科研绘图美化、写作润色、需要理解图形科学含义的任务（本 Skill 只把图表当结构元素定位）。

## 运行环境（评测/Harbor 容器）

| 位置 | 内容 |
|-|-|
| `/input/case.json` | 案例定义：`case_id`、任务说明、材料路径、`output_contract`（必须回填的字段） |
| `/input/task.md` | 本次任务的自然语言要求 |
| 材料 | 与 `case.json` 同级的 PDF；以实际文件为准，不要根据标签猜内容 |
| `/app/work` | 中间文件（answers.json、临时日志） |
| `/app/output` | 最终结果：`result.json` + `structure/` + `previews/` |

依赖：Python 3.10+，`pymupdf`（必需，读取文本层与渲染），`pdfplumber`（可选，表格单元格），`pytesseract`+`Pillow`（可选，扫描件 OCR）。缺少可选依赖时脚本会降级并在 `meta.json` 里写明，不会中断。

## 标准流程

1. **读案例**：先读 `/input/case.json` 和 `/input/task.md`，记下 `case_id`、问题清单、`output_contract` 字段名、页数/语言/材料数量。契约决定交付结构，题面决定答案内容。
2. **解析**：

   ```bash
   python scripts/parse_document.py --input /input --out /app/output
   # 单文件: --pdf /input/paper.pdf
   ```

   产出 `structure/document.md`（阅读顺序正文 + 每段锚点）、`structure/blocks.jsonl`（逐块：页、bbox、角色、章节路径）、`structure/tables/*.md`、`structure/tables.json`、`structure/references.json`、`structure/meta.json`、`previews/page-*.png`。
3. **先验版式，再答问题**：打开 `meta.json` 看 `column_models`（每页栏数与栏缝位置）、`coverage`（文本层字符捕获率）、`flow_share`、`warnings`；再抽查 1–2 张 `previews` 页图，确认双栏页确实是"先左栏到底、再右栏"，而不是按行交错。
4. **取证据**：

   ```bash
   python scripts/evidence_search.py --blocks /app/output/structure/blocks.jsonl --query "问题原文"
   ```

   返回按 BM25 排序的段落，带 `block_id`、`page`、`bbox`、`excerpt`。用它定位原文，不要把检索结果直接当答案。
5. **写答案**：把每个问题的结论写进 `/app/work/answers.json`（格式见下），每条结论必须挂 `citations`（页码 + block_id + 原文引句）。原文没有的，写 `not_reported`；材料互相矛盾，写 `conflict` 并列出两处证据。
6. **回填契约**：

   ```bash
   python scripts/make_result.py --case /input/case.json --structure /app/output/structure \
       --answers /app/work/answers.json --out /app/output/result.json
   ```

   契约里每个字段都会被填：能由解析结果支撑的自动填（`text`/`sections`/`tables`/`figures`/`references`/`metadata`），只能由人判断的取 `answers.json`，两边都没有的置 `null` 并写入 `uncertainty`，同时把 `status` 降为 `partial`。
7. **自检**：

   ```bash
   python scripts/selfcheck.py --result /app/output/result.json --structure /app/output/structure
   ```

   `PASS` 才交付；`FAIL` 按提示改（空契约字段、页码越界、引句与区块不一致、上下标标签不闭合）。预算紧时先把已确认部分落盘为 `partial`，再逐步补。

## answers.json 格式

```json
{
  "status": "success",
  "answers": [
    {
      "question": "作者用什么指标比较检测速度？",
      "answer": "用 mAP 与 inference time (ms) 两个量对照；YOLOv3-320 为 28.2 mAP / 22 ms。",
      "certainty": "reported",
      "citations": [
        {"page": 1, "block_id": "b18", "quote": "28.0 28.0 29.9 31.2 ..."}
      ]
    }
  ],
  "uncertainty": ["第 4 页表格跨页续接需人工核对首行表头"]
}
```

`certainty` 取值：`reported`（原文明确）/ `inferred`（由原文数据直接推出，写明推理）/ `not_reported`（原文未给）/ `conflict`（两处材料冲突）。

## 证据纪律（评分最看重的一条）

- 引句必须**逐字**来自 `blocks.jsonl` 的 `text`；数字、单位、图号、表号照抄，不改写、不补全。
- 每个结论至少一条 `citations`；找不到就写 `not_reported`，不许用常识填空。
- 区分三种"没有"：**未报告**（原文没提）、**无法判断**（材料残缺/加密/扫描无文字层）、**材料冲突**（两处不一致）。三者都不等于"答案是 X"。
- 表格结论引用 `table-p{页}-{序号}` 锚点，并说明是哪一行哪一列；跨页表格注明续表页码。
- 公式按原文符号转录（含 `<sup>`/`<sub>` 标记）；不做 LaTeX 结构还原时必须在 `uncertainty` 里说明。

## 异常处理

| 情况 | 识别 | 处置 |
|-|-|-|
| 加密 PDF | `parse_runs.json` 报 `encrypted PDF` | `status: refuse`，写明需要密码，不猜内容 |
| 无文字层扫描件 | 页文本为空、`meta.warnings` 提示 | 有 `pytesseract` 时自动 OCR，结果标 `ocr` 并提示需人工校对；无 OCR 时写 `无法判断` |
| 文件损坏/截断 | `pages: 0` 或解析异常 | 如实报告文件问题，不产出编造正文 |
| 只用可选依赖缺失 | `meta.parser` 显示 tables: disabled | 继续跑，说明表格以文本块呈现，需人工结构化 |
| 页数过多/预算紧 | 页数 > 30 | 先 `--page-limit` 解析目标页段落，再补全；`uncertainty` 注明未覆盖页 |
| 栏数判断可疑 | `column_models` 与预览图不一致 | 用 `--no-previews false` 重跑并人工核对，必要时按 `--page-limit` 逐页确认后再答 |

## 最小示例（示例，不是正式案例结果）

输入：一篇双栏论文 `example.pdf`（2 页）。

```bash
python scripts/parse_document.py --pdf example.pdf --out /app/output
python scripts/evidence_search.py --blocks /app/output/structure/blocks.jsonl --query "batch size"
```

示意输出（**示例**）：

```json
{"query": "batch size", "passages": [
  {"block_id": "b31", "page": 2, "bbox": [308.9, 411.2, 545.1, 470.6],
   "section": "3. Experiments > 3.1 Setup", "score": 7.84,
   "excerpt": "…we use a batch size of 256 and a weight decay of 0.0001…"}]}
```

```json
{"case_id": "example-case", "status": "success",
 "output_contract": {"answer": {"text": "…"}, "evidence": [{"page": 2, "block_id": "b31"}]}}
```

## 限制

- 旋转文字（页边竖排印章、竖排边注）不进入正文流，仅保留在 `blocks.jsonl`（`running`/被剔除时见 `meta`）。
- 未做跨页表格的自动合并；跨页续表以独立表格对象输出，需在答案中说明续接关系。
- 公式只做**字符保真**（上下标、希腊字母、编号），不做 LaTeX 语义还原；图片公式不转写。
- 扫描件 OCR 质量取决于图像分辨率与 tesseract 语言包，输出必须标注 `ocr` 并提示复核。
- 章节层级由字号、加粗与编号推断，非编号标题可能归到父级。

## 维护

新增版式样本时，把 PDF 放进 `examples/`，跑 `parse_document.py` 后核对 `meta.json` 的 `column_models` 与 `coverage`；改判定规则（`detect_gutters`、`classify_block`）后必须回归 `examples/` 下全部样本。
