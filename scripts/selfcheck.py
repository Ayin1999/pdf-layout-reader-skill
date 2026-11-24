#!/usr/bin/env python3
"""Self-check for a pdf-layout-reader run (pdf-layout-reader skill).

Gate before hand-in: the parsed artifacts must exist, the result must carry
case_id and every output_contract field, and every citation must resolve to a
real page and a real block. Prints PASS/FAIL with concrete fix hints.

Usage:
  python selfcheck.py --result /app/output/result.json --structure /app/output/structure
Exit code 0 = pass (warnings allowed), 1 = fail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

TAG_PAIR = re.compile(r"<sup>(.*?)</sup>|<sub>(.*?)</sub>")


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", default="/app/output/result.json")
    ap.add_argument("--structure", default="/app/output/structure")
    ap.add_argument("--strict", action="store_true", help="warnings become failures")
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    result = load_json(args.result)
    if result is None:
        print("FAIL: result.json missing or not valid JSON")
        return 1

    meta = load_json(os.path.join(args.structure, "meta.json"), {}) or {}
    pages = int(meta.get("pages") or 0)

    blocks = {}
    bpath = os.path.join(args.structure, "blocks.jsonl")
    if os.path.exists(bpath):
        with open(bpath, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    b = json.loads(line)
                    blocks[b.get("id")] = b
    else:
        errors.append("structure/blocks.jsonl missing: run parse_document.py first")

    if not result.get("case_id"):
        errors.append("result.case_id is empty; copy the id from /input/case.json")
    if result.get("status") not in ("success", "partial", "refuse", "failed"):
        warnings.append(f"unusual status {result.get('status')!r}; use success/partial/refuse/failed")

    contract = result.get("output_contract") or {}
    for field, value in contract.items():
        if value in (None, "", [], {}):
            has_note = any(field in u for u in (result.get("uncertainty") or []))
            if not has_note:
                errors.append(f"contract field '{field}' is empty and has no uncertainty entry")

    ev = result.get("evidence") or []
    if not ev and result.get("status") == "success":
        warnings.append("no evidence entries: every key claim should point back to a page")

    for item in ev:
        page = item.get("page")
        bid = item.get("block_id")
        if not isinstance(page, int) or (pages and not 1 <= page <= pages):
            errors.append(f"evidence with invalid page {page!r} (document has {pages} pages)")
        if bid and bid not in blocks:
            errors.append(f"evidence block_id {bid!r} not found in blocks.jsonl")
        if item.get("quote") and bid in blocks:
            quote = re.sub(r"\s+", "", item["quote"])
            body = re.sub(r"\s+", "", blocks[bid].get("text", ""))
            if quote[:40] and quote[:40] not in body:
                warnings.append(f"quote for {bid} is not a verbatim substring of that block")

    doc_md = os.path.join(args.structure, "document.md")
    if not os.path.exists(doc_md) or os.path.getsize(doc_md) < 200:
        errors.append("structure/document.md missing or nearly empty")
    else:
        text = open(doc_md, encoding="utf-8").read()
        if text.count("<sup>") != text.count("</sup>") or text.count("<sub>") != text.count("</sub>"):
            errors.append("unbalanced <sup>/<sub> tags in document.md: join logic broke a span")

    prev = os.path.join(os.path.dirname(args.structure), "previews")
    n_prev = len([f for f in os.listdir(prev)]) if os.path.isdir(prev) else 0
    if n_prev == 0:
        warnings.append("no page previews: rerun without --no-previews so a human can inspect layout")

    cov = meta.get("coverage")
    if isinstance(cov, (int, float)) and cov < 0.97:
        warnings.append(f"text_capture {cov} < 0.97: some glyphs in the text layer were not captured")

    if errors:
        print("FAIL")
        for e in errors:
            print("  error:", e)
        for w in warnings:
            print("  warn :", w)
        return 1
    if args.strict and warnings:
        print("FAIL (strict)")
        for w in warnings:
            print("  warn :", w)
        return 1
    print("PASS")
    for w in warnings:
        print("  warn:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
