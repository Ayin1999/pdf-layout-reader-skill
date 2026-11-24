#!/usr/bin/env python3
"""Assemble result.json for a reading/parsing case (pdf-layout-reader skill).

The evaluation harness hands each case as /input/case.json. Its `output_contract`
decides which fields the answer must expose; this script fills every contract
field from either
  * the structured parse (document.md, blocks, tables, references), or
  * an answers.json the operating agent wrote after reading the evidence,
and keeps page/bbox provenance next to each answer.

Nothing is invented: a field the materials do not support is emitted as null
with an explicit uncertainty entry, and the run status becomes `partial`.

Usage:
  python make_result.py --case /input/case.json --structure /app/output/structure \
      --answers /app/work/answers.json --out /app/output/result.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

PAGE_ANCHOR = re.compile(r"<!-- \{#(b\d+) p(\d+) bbox=([\d\.,]+)\} -->")

SECTION_HINTS = ("section", "outline", "structure", "hierarchy", "章节", "结构")
TABLE_HINTS = ("table", "表格")
FIGURE_HINTS = ("figure", "caption", "图表", "图注")
REF_HINTS = ("reference", "bibliograph", "citation", "参考")
TEXT_HINTS = ("text", "markdown", "content", "transcript", "full_text", "正文", "全文")
META_HINTS = ("metadata", "title", "author", "doi", "journal", "摘要", "abstract", "题名")


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def find_contract(node, depth: int = 0):
    """Depth-first search for the output contract anywhere in case.json."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k).lower()
            if any(word in key for word in ("output_contract", "outputcontract", "output_schema",
                                            "answer_contract", "required_fields", "required_outputs")):
                return v
        for v in node.values():
            found = find_contract(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = find_contract(v, depth + 1)
            if found is not None:
                return found
    return None


def contract_fields(contract) -> list[str]:
    if contract is None:
        return []
    if isinstance(contract, dict):
        return [str(k) for k in contract.keys()]
    if isinstance(contract, list):
        out = []
        for item in contract:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("name", "field", "key", "id"):
                    if key in item:
                        out.append(str(item[key]))
                        break
        return out
    if isinstance(contract, str):
        return [contract]
    return []


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="/input/case.json")
    ap.add_argument("--structure", default="/app/output/structure")
    ap.add_argument("--answers", default=None)
    ap.add_argument("--out", default="/app/output/result.json")
    ap.add_argument("--extra-uncertainty", default=None, help="text appended to the uncertainty list")
    args = ap.parse_args()

    case = load_json(args.case, {}) or {}
    answers = load_json(args.answers, {}) or {}
    meta = load_json(os.path.join(args.structure, "meta.json"), {}) or {}
    tables = load_json(os.path.join(args.structure, "tables.json"), []) or []
    refs = load_json(os.path.join(args.structure, "references.json"), []) or []
    blocks = []
    bpath = os.path.join(args.structure, "blocks.jsonl")
    if os.path.exists(bpath):
        with open(bpath, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    blocks.append(json.loads(line))
    doc_md = read_text(os.path.join(args.structure, "document.md"))

    case_obj = case.get("case") if isinstance(case.get("case"), dict) else {}
    case_id = (case.get("case_id") or case.get("id") or case.get("caseId")
               or case_obj.get("case_id") or case_obj.get("id"))
    contract = find_contract(case)
    fields = contract_fields(contract)

    headings = [b for b in blocks if b.get("role") in ("heading", "title")]
    captions = [b for b in blocks if b.get("role") == "caption"]

    def table_md(t: dict) -> str:
        rows, ncols = t["rows"], t["n_cols"]
        head = rows[0] + [""] * (ncols - len(rows[0]))
        out = ["| " + " | ".join(c.replace("|", "\\|") for c in head) + " |",
               "| " + " | ".join(["---"] * ncols) + " |"]
        for r in rows[1:]:
            cells = r + [""] * (ncols - len(r))
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        return "\n".join(out)

    auto = {
        "text": doc_md,
        "sections": [{"level": b.get("level", 1), "title": b["text"], "page": b["page"],
                      "block_id": b["id"], "bbox": b["bbox"]} for b in headings],
        "outline": [{"level": b.get("level", 1), "title": b["text"], "page": b["page"]} for b in headings],
        "tables": [{"page": t["page"], "index": t["index"], "bbox": t["bbox"],
                    "rows": t["n_rows"], "cols": t["n_cols"], "markdown": table_md(t)}
                   for t in tables],
        "figures": [{"caption": c["text"], "page": c["page"], "block_id": c["id"], "bbox": c["bbox"]}
                    for c in captions],
        "references": refs,
        "metadata": {"pages": meta.get("pages"), "pdf": meta.get("pdf"),
                     "title": next((b["text"] for b in blocks if b.get("role") == "title"), None),
                     "authors": next((b["text"] for b in blocks if b.get("role") == "authors"), None)},
        "pages": meta.get("pages"),
    }

    def fill(field: str):
        key = field.lower()
        if key in ("case_id", "caseid", "case"):
            return case_id
        if any(h in key for h in TABLE_HINTS):
            return auto["tables"]
        if any(h in key for h in SECTION_HINTS):
            return auto["sections"] if "section" in key else auto["outline"]
        if any(h in key for h in FIGURE_HINTS):
            return auto["figures"]
        if any(h in key for h in REF_HINTS):
            return auto["references"]
        if any(h in key for h in TEXT_HINTS):
            return auto["text"]
        if any(h in key for h in META_HINTS):
            return auto["metadata"]
        if key in ("pages", "page_count", "n_pages"):
            return auto["pages"]
        if any(h in key for h in ("answer", "question", "qa", "结果", "问答")):
            return answers.get("answers")
        if any(h in key for h in ("evidence", "source", "citation", "locator", "依据", "证据")):
            return answers.get("evidence") or [c for a in (answers.get("answers") or [])
                                               for c in (a.get("citations") or [])]
        if key in ("status", "运行状态"):
            return answers.get("status") or "success"
        if key in ("uncertainty", "限制", "不确定"):
            return answers.get("uncertainty") or []
        if field in (answers.get("fields") or {}):
            return answers["fields"][field]
        # explicit answers win over automatic mapping for anything unmapped
        fields_map = answers.get("fields") or {}
        for k, v in fields_map.items():
            if k.lower() == key:
                return v
        return None

    results = {}
    missing = []
    for f in fields:
        value = fill(f)
        results[f] = value
        if value in (None, "", [], {}):
            missing.append(f)

    status = answers.get("status") or ("partial" if missing else "success")
    uncertainty = list(answers.get("uncertainty") or [])
    if missing:
        uncertainty.append("合同字段未被材料支持，已置空: " + ", ".join(missing))
    if meta.get("warnings"):
        uncertainty.extend(meta["warnings"][:3])
    if args.extra_uncertainty:
        uncertainty.append(args.extra_uncertainty)

    evidence = answers.get("evidence") or []
    if not evidence:  # fall back to the citation list carried by each answer
        evidence = [c for a in (answers.get("answers") or []) for c in (a.get("citations") or [])]

    result = {
        "case_id": case_id,
        "status": status,
        "output_contract": results,
        "evidence": evidence,
        "uncertainty": uncertainty,
        "notes": answers.get("notes") or [],
        "artifacts": {
            "document_md": os.path.join(args.structure, "document.md"),
            "blocks_jsonl": bpath,
            "tables": os.path.join(args.structure, "tables.json"),
            "references": os.path.join(args.structure, "references.json"),
            "meta": os.path.join(args.structure, "meta.json"),
            "previews": os.path.join(os.path.dirname(args.structure), "previews"),
        },
        "run": {"parser": meta.get("parser"), "pages": meta.get("pages"),
                "coverage": meta.get("coverage"), "flow_share": meta.get("flow_share"),
                "elapsed_s": meta.get("elapsed_s")},
        "change_log": [{"ts": None, "change": "result assembled from structured parse"
                        if not answers else "result assembled from answers + structured parse"}],
    }
    # tolerate graders that read the contract fields at the top level
    for k, v in results.items():
        result.setdefault(k, v)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"written": args.out, "case_id": case_id, "status": status,
                      "fields": len(fields), "missing": missing}, ensure_ascii=False))
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
