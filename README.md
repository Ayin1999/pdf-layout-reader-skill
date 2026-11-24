# pdf-layout-reader

科研 PDF 的结构化解析 Skill：把论文变成带页码/区块定位的正文、章节树、表格、题注、参考文献与证据片段。

## 目录

```
SKILL.md                  Skill 说明（触发条件、流程、证据纪律、异常处理）
scripts/parse_document.py 主解析器：版面分析 → 阅读顺序 → 角色分类 → 产物落盘
scripts/evidence_search.py 证据检索：BM25（中文按字符二元组，英文按词）定位段落
scripts/make_result.py   按 case.json 的 output_contract 组装 result.json
scripts/selfcheck.py     交付前自检（页码、引句、契约字段、标签闭合）
examples/                 示例输入/输出（标注为示例，不是正式案例答案）
```

## 依赖

| 包 | 必需 | 用途 |
|-|-|-|
| `pymupdf`（PyMuPDF 1.24+） | 是 | 文本层、字体/字号、上下标标志、页面渲染、矢量笔画聚类 |
| `pdfplumber` 0.11+ | 否 | 表格单元格（`find_tables()`），缺失时表格退化为文本块 |
| `pytesseract` + `Pillow` | 否 | 扫描件 OCR，缺失时无文字层页面标记为"无法判断" |

```bash
python -m pip install pymupdf pdfplumber        # 最小可用
python -m pip install pytesseract pillow        # 需要 OCR 时
```

## 用法

```bash
# 1) 解析（--input 目录递归找 *.pdf，或 --pdf 指定单文件）
python scripts/parse_document.py --input /input --out /app/output
python scripts/parse_document.py --pdf paper.pdf --out out --page-limit 8 --no-previews

# 2) 定位证据
python scripts/evidence_search.py --blocks out/structure/blocks.jsonl --query "learning rate"

# 3) 按契约组装结果（answers.json 由使用 Skill 的 Agent 撰写）
python scripts/make_result.py --case /input/case.json --structure out/structure \
    --answers work/answers.json --out out/result.json

# 4) 自检
python scripts/selfcheck.py --result out/result.json --structure out/structure
```

## 方法要点

1. **阅读顺序**：从块左边界聚类推断栏位（标题/作者横跨两栏不会污染判断），再用"跨栏块"把页面切成带状区域，带内按栏从左到右、栏内按 y 排序。单栏页只按 y 排序。
2. **页眉页脚**：页面顶部 10% / 底部 10% 内、跨页重复（数字归一化后）或纯页码的块标为 `running`，不进正文。
3. **角色分类**：`title`、`authors`、`heading`（字号/加粗/编号推断层级）、`body`、`caption`（Fig./Table/图/表 开头）、`figure_label`（落在图片或矢量笔画聚类内的小字）、`table_text`（落在 pdfplumber 表格框内）、`footnote`（底部小字）、`reference`（References 之后按编号切条）。
4. **保真**：span 级别的 `<sup>`/`<sub>` 标记、连字符断词合并、CJK 不加多余空格、希腊字母与公式符号原样保留。
5. **两套质量信号**：`coverage`（文本层字符捕获率，反映是否漏字）与 `flow_share`（进入正文流的比例，差额是页眉、图内标签、表格单元格——它们仍在 `blocks.jsonl` 里可定位）。

## 已知限制

- 旋转文字不进入正文流；跨页表格不自动合并；公式只做字符保真，不做 LaTeX 语义还原；OCR 依赖本地 tesseract 语言包。
- 章节层级由字号/编号推断，未编号的小标题可能并入父级。

## 回归自测

`examples/` 下放了 3 个真实公开论文样本（双栏 CVPR、双栏技术报告、单栏长文）的解析摘要。改动判定规则后跑：

```bash
python scripts/parse_document.py --pdf examples/sample_two_column.pdf --out /tmp/a
python scripts/selfcheck.py --result examples/example_result.json --structure /tmp/a/structure
```

验收标准：`coverage ≥ 0.97`；双栏样本 `column_models` 的 `columns` 为 2；`previews` 中第 1 页的段落顺序与原文一致。
