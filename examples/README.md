# 示例（示例，不是正式案例结果）

本目录只放**示意性**输入输出，用于说明接口与交付结构；正式案例与对照由评测平台提供，这里不含任何正式案例答案。

| 文件 | 说明 |
|-|-|
| `example_case.json` | 一个示例 case.json：`case_id`、问题、材料清单、`output_contract` |
| `example_answers.json` | 示例 answers.json：每题带 `citations`（页码 + block_id + 原文引句） |
| `example_result.json` | 示例 result.json 的**截断版**（保留字段结构，正文与表格内容省略，由 `make_result.py` 现场生成） |
| `example_run.log` | 一次真实开发运行（公开论文 ResNet，12 页）的命令与输出摘要 |

复现方式（示例 PDF 自备，如 arXiv:1512.03385）：

```bash
python scripts/parse_document.py --pdf paper.pdf --out /app/output
python scripts/evidence_search.py --blocks /app/output/structure/blocks.jsonl --query "degradation problem"
python scripts/make_result.py --case example_case.json --structure /app/output/structure \
    --answers example_answers.json --out /app/output/result.json
python scripts/selfcheck.py --result /app/output/result.json --structure /app/output/structure
```

示例输出中的页码、block_id、bbox 都来自该次真实解析，便于核对锚点格式；它们**不代表**评测案例的结果。
