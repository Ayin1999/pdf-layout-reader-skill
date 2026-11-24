#!/usr/bin/env python3
"""Evidence retrieval over a parsed PDF (pdf-layout-reader skill).

Turns a question into ranked passages that carry a page number, a block id and
the original bounding box, so an answer can always be traced back to the sheet.

Works with Chinese and English queries: CJK runs are indexed as character
bigrams (no word segmenter needed), Latin text as lower-cased words, scored with
BM25 over the block collection produced by parse_document.py.

Usage:
  python evidence_search.py --blocks structure/blocks.jsonl --query "训练用了多少层"
  python evidence_search.py --blocks structure/blocks.jsonl --questions q.json --out .
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter

CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
LATIN = re.compile(r"[a-z0-9]+(?:[-_'][a-z0-9]+)*")
CLEAN = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+")


def tokens(text: str) -> list[str]:
    text = (text or "").lower()
    out: list[str] = []
    for run in CJK_RUN.findall(text):
        out.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    out.extend(LATIN.findall(text))
    return out


class Index:
    def __init__(self, blocks: list[dict]):
        self.blocks = [b for b in blocks if b.get("text")]
        self.tfs = [Counter(tokens(b["text"])) for b in self.blocks]
        self.len = [sum(tf.values()) for tf in self.tfs]
        self.avg = sum(self.len) / max(1, len(self.len))
        df = Counter()
        for tf in self.tfs:
            df.update(set(tf))
        n = len(self.blocks)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, top: int = 5, section: str | None = None,
               roles: set | None = None) -> list[dict]:
        q = tokens(query)
        if not q:
            return []
        k1, b = 1.4, 0.75
        hits = []
        for i, tf in enumerate(self.tfs):
            blk = self.blocks[i]
            if section and section.lower() not in (blk.get("section") or "").lower():
                continue
            if roles and blk.get("role") not in roles:
                continue
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                score += self.idf.get(t, 0.0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * self.len[i] / self.avg))
            if not score:
                continue
            # a literal phrase hit is worth more than the sum of its bigrams
            if len(q) > 1 and query.strip().lower() in blk["text"].lower():
                score *= 1.35
            hits.append((score, i))
        hits.sort(reverse=True)
        out = []
        for score, i in hits[:top]:
            blk = self.blocks[i]
            out.append({
                "block_id": blk.get("id"),
                "page": blk.get("page"),
                "bbox": blk.get("bbox"),
                "role": blk.get("role"),
                "section": blk.get("section"),
                "score": round(score, 3),
                "text": blk["text"],
                "excerpt": excerpt(blk["text"], q),
            })
        return out


def excerpt(text: str, q_tokens: list[str], width: int = 320) -> str:
    low = text.lower()
    best_pos, best_hits = 0, -1
    for start in range(0, max(1, len(text) - width) + 1, max(1, width // 6)):
        window = low[start:start + width]
        hits = sum(window.count(t) for t in q_tokens)
        if hits > best_hits:
            best_hits, best_pos = hits, start
    if len(text) <= width:
        return text
    seg = text[best_pos:best_pos + width]
    return ("…" if best_pos else "") + seg + ("…" if best_pos + width < len(text) else "")


def load_blocks(path: str) -> list[dict]:
    blocks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                blocks.append(json.loads(line))
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blocks", default="/app/output/structure/blocks.jsonl")
    ap.add_argument("--query")
    ap.add_argument("--questions", help="JSON file: [\"q1\", ...] or {\"questions\": [...]}")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--section", default=None)
    ap.add_argument("--out", default=None, help="write JSON here; default stdout")
    args = ap.parse_args()

    blocks = load_blocks(args.blocks)
    index = Index(blocks)
    roles = {"body", "heading", "caption", "footnote", "reference", "title", "authors", "table_text"}

    if args.questions:
        with open(args.questions, encoding="utf-8") as fh:
            payload = json.load(fh)
        qs = payload.get("questions", []) if isinstance(payload, dict) else payload
        result = {"queries": [{"question": q, "passages": index.search(q, args.top, args.section, roles)}
                              for q in qs]}
    else:
        if not args.query:
            print("need --query or --questions", file=sys.stderr)
            return 2
        result = {"query": args.query, "passages": index.search(args.query, args.top, args.section, roles)}

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(json.dumps({"written": args.out, "passages": len(result.get("passages", []))}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
