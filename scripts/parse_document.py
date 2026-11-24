#!/usr/bin/env python3
"""Deterministic PDF structure parser (pdf-layout-reader skill).

Reads one PDF and writes layout-aware artifacts:
  structure/document.md   human readable, reading-order restored
  structure/blocks.jsonl  one JSON object per block, with page + bbox provenance
  structure/tables/*.md   table markdown, row/column provenance kept in tables.json
  structure/meta.json     page geometry, column model, coverage self-check, warnings
  previews/page-*.png     rendered page images for visual verification

Design notes (what this fixes compared with `pdftotext -layout`):
  1. reading order: gutters are found from block coverage, full-width blocks
     (title, abstract, wide tables) break the page into bands, then each band
     is emitted column by column instead of line by line;
  2. running heads / page numbers are detected by repetition and kept out of body;
  3. superscripts/subscripts keep <sup>/<sub> markers instead of collapsing;
  4. every block carries (page, bbox) so a reader can point back to the sheet.

Usage:
  python parse_document.py --pdf paper.pdf --out /app/output
  python parse_document.py --input /input --out /app/output   # scans for PDFs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:  # older wheels expose only `fitz`
    import fitz as pymupdf  # type: ignore

pdfplumber = None
try:
    import pdfplumber  # optional, only used for table cells
except Exception:  # pragma: no cover
    pdfplumber = None

CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
SENT_END = re.compile(r"[.。!?！？:：;；]\s*$")
HYPHEN_END = re.compile(r"[‐-]$")
PAGE_NUM = re.compile(r"^\s*(page\s*)?[\divxlcdm]{1,6}\s*$", re.IGNORECASE)
CAPTION = re.compile(
    r"^\s*((fig|figure|tab|table|图表|图|表|scheme|algorithm)\s*[\.\s]*\d+[a-z]?[\s\.:：]|"
    r"^(fig|figure|tab|table|图|表)\s+[IVXivx\d]+[a-z]?[\s\.:：])",
    re.IGNORECASE,
)
NUMBERING = re.compile(r"^\s*(\d+(\.\d+)*)[\.\s、]\s*(.+)$")
REF_HEAD = re.compile(r"^(references|bibliography|literature cited|参考文献|引用文献)\s*$", re.IGNORECASE)
ABSTRACT_HEAD = re.compile(r"^(abstract|摘要)\s*[:：]?\s*$", re.IGNORECASE)
REF_ITEM = re.compile(r"^\s*([\[\d]+\]|\d+[\.\)])\s+")
SECTION_WORDS = {
    "abstract", "摘要", "introduction", "引言", "绪论", "background", "related work",
    "method", "methods", "方法", "approach", "experiments", "experiment", "实验",
    "results", "结果", "discussion", "讨论", "conclusion", "conclusions", "结论",
    "references", "参考文献", "acknowledgement", "acknowledgements", "致谢",
    "appendix", "附录", "supplementary", "limitations", "future work",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def join_lines(prev: str, nxt: str) -> str:
    """Join two physical lines without inventing spaces for CJK text."""
    if not prev:
        return nxt
    if HYPHEN_END.search(prev) and prev[-1] in "-‐":
        return prev[:-1] + nxt
    if not nxt:
        return prev
    a, b = prev[-1], nxt[0]
    if CJK.match(a) or CJK.match(b):
        return prev + nxt
    return prev + " " + nxt


def span_text(span) -> str:
    """Keep superscript/subscript structure instead of flattening it."""
    text = span.get("text", "")
    if not text:
        return ""
    flags = span.get("flags", 0)
    if flags & 1:  # MuPDF superscript flag
        return f"<sup>{text}</sup>"
    return text


def block_from_raw(raw_block, page_no: int, page_h: float, body_size: float) -> dict:
    lines, md_lines, sizes, bolds, fonts = [], [], [], [], []
    for line in raw_block.get("lines", []):
        direction = line.get("dir", (1, 0))
        if abs(direction[0] - 1) > 0.01 or abs(direction[1]) > 0.01:
            continue  # rotated text (margin stamps, sidebars) is not body prose
        spans = line.get("spans", [])
        if not spans:
            continue
        base_size = max((s.get("size", 0) for s in spans), default=0)
        base_y = max((s.get("origin", (0, 0))[1] for s in spans if s.get("size") == base_size), default=0)
        line_text, line_md = "", ""
        for sp in spans:
            t = sp.get("text", "")
            if not t:
                continue
            line_text = join_lines(line_text, t) if line_text else t
            size = sp.get("size", 0)
            oy = sp.get("origin", (0, 0))[1]
            if sp.get("flags", 0) & 1:
                piece = f"<sup>{t}</sup>"
            elif size and base_size and size < 0.80 * base_size and oy > base_y + 0.10 * base_size:
                piece = f"<sub>{t}</sub>"  # subscript: smaller glyph sitting below the baseline
            else:
                piece = t
            line_md = (line_md + piece) if line_md else piece
            sizes.append(size or 0)
            bolds.append(bool(sp.get("flags", 0) & 8) or "bold" in sp.get("font", "").lower())
            fonts.append(sp.get("font", ""))
        if line_text:
            lines.append(line_text)
            md_lines.append(line_md)

    text = ""
    for ln in lines:
        text = join_lines(text, ln)
    md = ""
    for ln in md_lines:
        md = join_lines(md, ln)

    size = Counter([round(s, 1) for s in sizes if s]).most_common(1)
    size = size[0][0] if size else body_size
    bbox = raw_block.get("bbox", [0, 0, 0, 0])
    return {
        "page": page_no,
        "bbox": [round(v, 2) for v in bbox],
        "text": norm(text),
        "md": norm(md),
        "size": size,
        "bold": (sum(bolds) / len(bolds)) > 0.5 if bolds else False,
        "font": Counter(fonts).most_common(1)[0][0] if fonts else "",
        "y0": bbox[1],
        "y1": bbox[3],
        "x0": bbox[0],
        "x1": bbox[2],
        "width": bbox[2] - bbox[0],
    }


def body_font_size(doc) -> float:
    sizes = Counter()
    for page in doc[: min(3, len(doc))]:
        for raw in page.get_text("dict").get("blocks", []):
            if raw.get("type") != 0:
                continue
            for line in raw.get("lines", []):
                for sp in line.get("spans", []):
                    n = len(sp.get("text", "").strip())
                    if n:
                        sizes[round(sp.get("size", 0), 1)] += n
    return sizes.most_common(1)[0][0] if sizes else 10.0


def classify_running_blocks(pages_blocks: list[list[dict]], page_h: float) -> None:
    """Mark repeated top/bottom strings and bare page numbers as running heads."""
    counts = Counter()
    for blocks in pages_blocks:
        seen = set()
        for b in blocks:
            if b["y1"] < page_h * 0.10 or b["y0"] > page_h * 0.90:
                key = re.sub(r"\d+", "#", b["text"].lower())
                if key and key not in seen:
                    counts[key] += 1
                    seen.add(key)
    limit = max(3, int(0.30 * len(pages_blocks)))
    for blocks in pages_blocks:
        for b in blocks:
            if not (b["y1"] < page_h * 0.10 or b["y0"] > page_h * 0.90):
                continue
            key = re.sub(r"\d+", "#", b["text"].lower())
            if PAGE_NUM.match(b["text"]) or counts.get(key, 0) >= limit:
                b["role"] = "running"
            elif b["y0"] > page_h * 0.90 and b["size"] < 8.5:
                b["role"] = "footnote"


def detect_gutters(blocks: list[dict], page_w: float, page_h: float = 792.0) -> list[float]:
    """Column gutters from clusters of block left edges.

    Coverage histograms fail as soon as one title or author line spans both
    columns, so columns are inferred from where text *starts* instead: a real
    column produces several blocks sharing (almost) the same left edge. The
    gutter is then placed in the empty strip between the previous column's
    right edge and the next column's left edge.
    """
    narrow = [b for b in blocks if b["width"] < 0.60 * page_w and len(b["text"]) > 3]
    if len(narrow) < 4:
        return []
    xs = sorted(round(b["x0"], 1) for b in narrow)
    clusters: list[list[float]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= 8:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    edges = [sum(c) / len(c) for c in clusters if len(c) >= 3]
    if len(edges) < 2:
        return []
    gutters = []
    for e0, e1 in zip(edges, edges[1:]):
        if e1 - e0 < 0.18 * page_w:
            continue
        left = [b for b in blocks if b["x0"] < e1 - 6 and b["x1"] <= e1 - 4]
        right = [b for b in blocks if b["x0"] >= e1 - 4]
        if len(left) < 3 or len(right) < 3:
            continue
        max_x1 = max(b["x1"] for b in left)  # blocks that cross the boundary are spans, not columns
        # a column is narrow by construction; full-width prose on one side means
        # this is a paragraph indent, not a two-column layout. Plot tick labels are
        # only a few points wide, so the width test uses paragraph-sized blocks only.
        import statistics
        para_left = [b for b in left if b["width"] >= 0.15 * page_w]
        para_right = [b for b in right if b["width"] >= 0.15 * page_w]
        if len(para_left) < 3 or len(para_right) < 3:
            continue
        share = min(len(para_left), len(para_right)) / (len(para_left) + len(para_right))
        if share < 0.25:  # a handful of captions beside a wide text block is not a column layout
            continue
        mw_left = statistics.median([b["width"] for b in para_left])
        mw_right = statistics.median([b["width"] for b in para_right])
        if max(mw_left, mw_right) > 0.55 * page_w:
            continue
        # both sides must run down the page together, otherwise it is a caption
        # block parked next to a figure, not a column
        ly = (min(b["y0"] for b in left), max(b["y1"] for b in left))
        ry = (min(b["y0"] for b in right), max(b["y1"] for b in right))
        if min(ly[1], ry[1]) - max(ly[0], ry[0]) < 0.25 * page_h:
            continue
        gutters.append(round((max_x1 + e1) / 2, 2))
    return sorted(gutters)


def order_page(blocks: list[dict], gutters: list[float], page_w: float, page_h: float) -> list[dict]:
    """Band-wise reading order: full-width elements split the page, columns inside a band."""
    body = [b for b in blocks if b.get("role") not in ("running",)]
    # a block counts as full width when it crosses a gutter or nearly spans the sheet
    def is_wide(b):
        if b["width"] > 0.62 * page_w:
            return True
        return any(b["x0"] < g < b["x1"] for g in gutters)
    wide = [b for b in body if is_wide(b)]
    narrow = [b for b in body if not is_wide(b)]

    def col_of(b: dict) -> int:
        cx = 0.5 * (b["x0"] + b["x1"])
        for i, g in enumerate(gutters):
            if cx < g:
                return i
        return len(gutters)

    full_sorted = sorted(wide, key=lambda b: b["y0"])
    bands = []
    cursor = 0.0
    for f in full_sorted:
        if f["y0"] - cursor > 2:
            bands.append(("cols", cursor, f["y0"]))
        bands.append(("full", f, f))
        cursor = max(cursor, f["y1"])
    bands.append(("cols", cursor, page_h + 1))

    out = []
    for kind, a, b in bands:
        if kind == "full":
            out.append(b)
            continue
        members = [x for x in narrow if a <= 0.5 * (x["y0"] + x["y1"]) < b]
        if not members:
            continue
        groups = defaultdict(list)
        for x in members:
            groups[col_of(x)].append(x)
        for col in sorted(groups):
            out.extend(sorted(groups[col], key=lambda y: (round(y["y0"], 1), y["x0"])))
    # anything not yet placed (e.g. blocks overlapping a wide element) keeps y order at the end
    placed = {id(x) for x in out}
    leftovers = [x for x in body if id(x) not in placed]
    out.extend(sorted(leftovers, key=lambda y: (y["y0"], y["x0"])))
    for i, b in enumerate(out):
        b["order"] = i
    return out


def dedupe_tables(tables: list[dict]) -> list[dict]:
    """pdfplumber sometimes reports one visual table as several nested boxes."""
    def area(t):
        b = t["bbox"]
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    kept: list[dict] = []
    for t in sorted(tables, key=area, reverse=True):
        dup = False
        for k in kept:
            if k["page"] != t["page"]:
                continue
            a, b = t["bbox"], k["bbox"]
            inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            if inter / max(1e-6, area(t)) > 0.80:
                dup = True
                break
        if not dup:
            kept.append(t)
    return sorted(kept, key=lambda t: (t["page"], t["bbox"][1], t["bbox"][0]))


def table_bboxes(tables: list[dict], page: int) -> list[list[float]]:
    return [t["bbox"] for t in tables if t["page"] == page]


def classify_block(b: dict, body_size: float, in_refs: bool, page_images: list | None = None,
                   page_h: float = 792.0, table_boxes: list | None = None) -> str:
    t = b["text"]
    if not t:
        return "empty"
    if b.get("role") == "footnote":
        return "footnote"
    if b.get("role") == "title":
        return "title"
    if b.get("role") == "authors":
        return "authors"
    if CAPTION.match(t) and len(t) < 400:
        return "caption"  # a caption inside a figure region still names the figure
    # text sitting inside a plot (axis ticks, legends) is figure content, not prose
    cx, cy = 0.5 * (b["x0"] + b["x1"]), 0.5 * (b["y0"] + b["y1"])
    if table_boxes:
        for tb in table_boxes:
            if tb[0] - 2 <= cx <= tb[2] + 2 and tb[1] - 2 <= cy <= tb[3] + 2:
                return "table_text"  # cell text; the table itself is rendered from extract()
    if page_images and len(t) < 90:
        for rect in page_images:
            x0, y0, x1, y1 = rect
            plot_area = (x1 - x0) * (y1 - y0)
            blk_area = (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
            if x0 - 14 <= cx <= x1 + 14 and y0 - 14 <= cy <= y1 + 14:
                if b["size"] < body_size * 0.85 or blk_area < 0.15 * plot_area:
                    return "figure_label"
    if b.get("page") == 1 and b["y0"] < page_h * 0.45 and "@" in t:
        return "authors"
    if in_refs:
        return "reference"
    numbered = NUMBERING.match(t)
    short = len(t) <= 120 and t.count(". ") <= 1
    heading_like = (
        (b["size"] >= body_size * 1.12 and short)
        or (b["bold"] and short and (numbered or t.lower().strip(" .:：") in SECTION_WORDS))
        or ABSTRACT_HEAD.match(t) is not None
        or REF_HEAD.match(t.strip()) is not None
        or (numbered and b["bold"] and short)
    )
    if heading_like:
        return "heading"
    return "body"


def heading_level(text: str, body_size: float, size: float) -> int:
    m = NUMBERING.match(text)
    if m:
        return min(3, m.group(1).count(".") + 1)
    if size >= body_size * 1.45:
        return 1
    if text.strip(" .:：").lower() in SECTION_WORDS:
        return 1
    return 2


def extract_tables(pdf_path: str, max_pages: int | None = None) -> list[dict]:
    if pdfplumber is None:
        return []
    out = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
            for pno, page in enumerate(pages, start=1):
                try:
                    found = page.find_tables()
                except Exception:
                    continue
                for t_idx, table in enumerate(found, start=1):
                    try:
                        data = table.extract()
                    except Exception:
                        continue
                    rows = [[norm(c or "") for c in row] for row in data]
                    rows = [r for r in rows if any(r)]
                    if len(rows) < 2 or max(len(r) for r in rows) < 2:
                        continue  # a single column strip is a layout artefact, not a table
                    out.append({
                        "page": pno,
                        "index": t_idx,
                        "bbox": [round(v, 2) for v in table.bbox],
                        "rows": rows,
                        "n_rows": len(rows),
                        "n_cols": max(len(r) for r in rows),
                    })
    except Exception as exc:  # pdfplumber is optional and must never break the run
        print(f"[warn] table extraction skipped: {exc}", file=sys.stderr)
    return out


def table_to_markdown(table: dict) -> str:
    rows = table["rows"]
    n_cols = table["n_cols"]
    head = rows[0] + [""] * (n_cols - len(rows[0]))
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in head) + " |",
             "| " + " | ".join(["---"] * n_cols) + " |"]
    for row in rows[1:]:
        cells = row + [""] * (n_cols - len(row))
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def ocr_page(page, lang: str = "eng+chi_sim") -> str:
    """Optional OCR path; returns '' when tesseract is unavailable."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return norm(pytesseract.image_to_string(img, lang=lang))
    except Exception:
        return ""


def parse_pdf(pdf_path: str, out_dir: str, previews: bool = True, ocr: str = "auto",
              page_limit: int | None = None) -> dict:
    started = time.time()
    doc = pymupdf.open(pdf_path)
    if doc.needs_pass:
        raise PermissionError("encrypted PDF: text layer is not readable without a password")
    body_size = body_font_size(doc)
    pages = list(doc) if page_limit is None else list(doc)[:page_limit]
    tables = dedupe_tables(extract_tables(pdf_path, page_limit))  # before block roles: cells need their box

    pages_blocks: list[list[dict]] = []
    images_by_page: list[list[list[float]]] = []
    page_meta = []
    images = []
    for i, page in enumerate(pages, start=1):
        raw = page.get_text("dict")
        blocks = []
        page_images = []
        for b in raw.get("blocks", []):
            if b.get("type") == 1:
                rect = [round(v, 2) for v in b.get("bbox", [0, 0, 0, 0])]
                page_images.append(rect)
                images.append({"page": i, "bbox": rect})
                continue
            blk = block_from_raw(b, i, page.rect.height, body_size)
            if blk["text"]:
                blocks.append(blk)
        if i == 1:  # title lines can wrap: merge the largest contiguous type at the top
            cands = sorted([b for b in blocks if b["size"] >= body_size * 1.25
                            and b["y0"] < page.rect.height * 0.35], key=lambda b: b["y0"])
            groups: list[list[dict]] = []
            for b in cands:
                if groups and b["y0"] - groups[-1][-1]["y1"] < 1.8 * b["size"]:
                    groups[-1].append(b)
                else:
                    groups.append([b])
            if groups:
                best = max(groups, key=lambda g: max(x["size"] for x in g))
                if max(x["size"] for x in best) >= body_size * 1.35:
                    title_text = " ".join(x["text"] for x in best)
                    bbox = [min(x["x0"] for x in best), min(x["y0"] for x in best),
                            max(x["x1"] for x in best), max(x["y1"] for x in best)]
                    keep = best[0]
                    keep.update({"text": norm(title_text), "md": norm(title_text), "bbox": bbox,
                                 "y0": bbox[1], "y1": bbox[3], "x0": bbox[0], "x1": bbox[2],
                                 "width": bbox[2] - bbox[0], "role": "title",
                                 "size": max(x["size"] for x in best)})
                    for x in best[1:]:
                        blocks.remove(x)
                    # author / affiliation lines sit right below the title
                    below = sorted([b for b in blocks if b.get("role") != "title"
                                    and b["y0"] >= keep["y1"] - 2
                                    and b["y0"] < keep["y1"] + 0.10 * page.rect.height
                                    and b["size"] <= keep["size"]
                                    and not NUMBERING.match(b["text"])], key=lambda b: b["y0"])
                    for b in below:
                        b["role"] = "authors"
        pages_blocks.append(blocks)
        page_meta.append({"page": i, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2),
                          "rotation": page.rotation, "chars_raw": len(re.sub(r"\s+", "", page.get_text()))})
        # vector plots have no raster block: cluster their strokes so tick labels
        # inside a chart are tagged as figure content rather than body prose
        try:
            for rect in page.cluster_drawings():
                area = (rect.x1 - rect.x0) * (rect.y1 - rect.y0)
                if 0.004 * page.rect.width * page.rect.height < area < 0.85 * page.rect.width * page.rect.height:
                    page_images.append([round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)])
        except Exception:
            pass
        images_by_page.append(page_images)

    classify_running_blocks(pages_blocks, pages[0].rect.height if pages else 842)

    ordered_all: list[dict] = []
    col_models = []
    block_no = 0
    in_refs = False
    section_stack: list[str] = []
    for idx, blocks in enumerate(pages_blocks):
        page = pages[idx]
        page_w, page_h = page.rect.width, page.rect.height
        body_blocks = [b for b in blocks if b.get("role") != "running"]
        gutters = detect_gutters(body_blocks, page_w, page_h)
        col_models.append({"page": idx + 1, "gutters": gutters, "columns": len(gutters) + 1})
        ordered = order_page(blocks, gutters, page_w, page_h)

        # scanned page: no text layer at all
        if not blocks and ocr != "off":
            text = ocr_page(page)
            if text:
                ordered = [{"page": idx + 1, "bbox": [0, 0, page_w, page_h], "text": text, "md": text,
                            "size": body_size, "bold": False, "font": "", "x0": 0, "x1": page_w,
                            "y0": 0, "y1": page_h, "width": page_w, "order": 0, "role": "ocr"}]
                col_models[-1]["ocr"] = True
            else:
                page_meta[idx]["note"] = "no text layer and OCR unavailable"

        for b in ordered:
            b["role"] = classify_block(b, body_size, in_refs, images_by_page[idx], page_h,
                                       table_bboxes(tables, idx + 1))
            if b["role"] == "heading":
                b["level"] = heading_level(b["text"], body_size, b["size"])
                if REF_HEAD.match(b["text"].strip()):
                    in_refs = True
                # maintain a shallow section stack for anchors
                lvl = b.get("level", 1)
                section_stack = section_stack[: lvl - 1] + [b["text"][:80]]
                b["section"] = " > ".join(section_stack)
            else:
                b["section"] = " > ".join(section_stack)
            block_no += 1
            b["id"] = f"b{block_no}"
            b["column"] = len([g for g in gutters if 0.5 * (b["x0"] + b["x1"]) >= g])
            ordered_all.append(b)

        if previews:
            png_dir = os.path.join(out_dir, "previews")
            os.makedirs(png_dir, exist_ok=True)
            page.get_pixmap(dpi=110).save(os.path.join(png_dir, f"page-{idx + 1:04d}.png"))

    tables = tables or []
    table_dir = os.path.join(out_dir, "structure", "tables")
    os.makedirs(table_dir, exist_ok=True)
    for t in tables:
        with open(os.path.join(table_dir, f"p{t['page']:04d}-t{t['index']}.md"), "w", encoding="utf-8") as fh:
            fh.write(table_to_markdown(t) + "\n")

    # references: split a long reference block into numbered entries
    refs = []
    for b in ordered_all:
        if b.get("role") == "reference":
            parts = [p.strip() for p in REF_ITEM.split(b["text"]) if p and p.strip()]
            for p in parts:
                refs.append({"page": b["page"], "block": b["id"], "text": p})

    # coverage self-check: extracted characters vs the raw text layer
    FLOW_ROLES = {"body", "heading", "caption", "footnote", "reference", "title", "authors", "ocr",
                  "table_text"}
    extracted = sum(len(re.sub(r"\s+", "", b["md"])) for b in ordered_all if b.get("role") in FLOW_ROLES)
    captured_all = sum(len(re.sub(r"\s+", "", b["text"])) for blocks in pages_blocks for b in blocks)
    raw_chars = sum(pm["chars_raw"] for pm in page_meta)
    removed = sum(len(re.sub(r"\s+", "", b["text"])) for blocks in pages_blocks for b in blocks
                  if b.get("role") == "running")
    # two separate quality signals:
    #   text_capture -- did the extractor see every glyph of the text layer at all
    #   flow_share   -- how much of it reached the prose flow (rest = running heads,
    #                   plot labels, table cells, all of which stay addressable in blocks.jsonl)
    text_capture = round(min(1.0, captured_all / raw_chars), 3) if raw_chars else 0.0
    flow_share = round(min(1.0, extracted / max(1, raw_chars - removed)), 3) if raw_chars else 0.0
    coverage = text_capture

    doc_md_path = os.path.join(out_dir, "structure", "document.md")
    os.makedirs(os.path.dirname(doc_md_path), exist_ok=True)
    with open(doc_md_path, "w", encoding="utf-8") as fh:
        fh.write(f"<!-- source: {os.path.basename(pdf_path)}; pages: {len(pages)} -->\n\n")
        last_page = None
        for b in ordered_all:
            if b["page"] != last_page:
                fh.write(f"\n\n---\n\n**Page {b['page']}**\n\n")
                last_page = b["page"]
            if b.get("role") in ("figure_label", "running", "empty", "table_text"):
                continue  # kept in blocks.jsonl with provenance, kept out of the prose flow
            anchor = f"<!-- {{#{b['id']} p{b['page']} bbox={','.join(str(v) for v in b['bbox'])}}} -->"
            role = b.get("role")
            if role == "heading":
                fh.write(f"{'#' * (b.get('level', 1) + 1)} {b['text']} {anchor}\n\n")
            elif role == "title":
                fh.write(f"# {b['text']} {anchor}\n\n")
            elif role == "authors":
                fh.write(f"*{b['text']}* {anchor}\n\n")
            elif role == "caption":
                fh.write(f"*{b['text']}* {anchor}\n\n")
            elif role == "footnote":
                fh.write(f"> [footnote] {b['text']} {anchor}\n\n")
            elif role == "reference":
                fh.write(f"- {b['text']} {anchor}\n")
            else:
                fh.write(f"{b['md']} {anchor}\n\n")

        if tables:
            fh.write("\n\n---\n\n## 表格 / Tables\n\n")
            for t in tables:
                fh.write(f"### Table p.{t['page']}#{t['index']} "
                         f"<!-- {{#table-p{t['page']}-{t['index']} p{t['page']} "
                         f"bbox={','.join(str(v) for v in t['bbox'])}}} -->\n\n")
                fh.write(table_to_markdown(t) + "\n\n")

    with open(os.path.join(out_dir, "structure", "blocks.jsonl"), "w", encoding="utf-8") as fh:
        for b in ordered_all:
            fh.write(json.dumps({k: b.get(k) for k in
                                 ("id", "page", "bbox", "order", "column", "role", "size", "bold",
                                  "text", "md", "section")}, ensure_ascii=False) + "\n")

    warnings = []
    if text_capture < 0.97:
        warnings.append(f"text_capture {text_capture} < 0.97: glyphs in the text layer were not all captured")
    if flow_share < 0.70:
        warnings.append(f"flow_share {flow_share} < 0.70: much of the text did not reach the prose flow "
                        "(check figure labels / table cells before trusting the markdown)")
    if any(cm["columns"] > 1 for cm in col_models):
        warnings.append("multi-column layout detected: verify reading order against previews")
    if images and not any(CAPTION.match(b["text"]) for b in ordered_all):
        warnings.append("page images found without captions: figure numbering may be incomplete")
    if any("note" in pm for pm in page_meta):
        warnings.append("page without text layer: OCR result (if any) must be checked by hand")

    meta = {
        "pdf": os.path.basename(pdf_path),
        "sha256": hashlib.sha256(open(pdf_path, "rb").read()).hexdigest(),
        "pages": len(pages),
        "body_font_size": body_size,
        "column_models": col_models,
        "images": images,
        "tables": [{k: t[k] for k in ("page", "index", "bbox", "n_rows", "n_cols")} for t in tables],
        "references": len(refs),
        "coverage": coverage,
        "flow_share": flow_share,
        "warnings": warnings,
        "elapsed_s": round(time.time() - started, 2),
        "parser": {"engine": "PyMuPDF " + pymupdf.__doc__.split()[1] if pymupdf.__doc__ else "PyMuPDF",
                   "tables": "pdfplumber" if pdfplumber else "disabled"},
    }
    with open(os.path.join(out_dir, "structure", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "structure", "references.json"), "w", encoding="utf-8") as fh:
        json.dump(refs, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "structure", "tables.json"), "w", encoding="utf-8") as fh:
        json.dump(tables, fh, ensure_ascii=False, indent=2)

    meta["blocks"] = len(ordered_all)
    return meta


def find_pdfs(input_dir: str) -> list[str]:
    found = []
    for root, _dirs, files in os.walk(input_dir):
        for name in sorted(files):
            if name.lower().endswith(".pdf"):
                found.append(os.path.join(root, name))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", help="single PDF to parse")
    ap.add_argument("--input", default=None, help="directory scanned recursively for *.pdf (default /input)")
    ap.add_argument("--out", default="/app/output", help="output root")
    ap.add_argument("--no-previews", action="store_true", help="skip page PNG rendering")
    ap.add_argument("--ocr", choices=["auto", "off"], default="auto")
    ap.add_argument("--page-limit", type=int, default=None)
    args = ap.parse_args()

    pdfs = [args.pdf] if args.pdf else find_pdfs(args.input or "/input")
    if not pdfs:
        print(json.dumps({"status": "failed", "reason": "no PDF under the input path"}, ensure_ascii=False))
        return 2
    os.makedirs(args.out, exist_ok=True)
    results = []
    for p in pdfs:
        try:
            meta = parse_pdf(p, args.out, previews=not args.no_previews, ocr=args.ocr,
                             page_limit=args.page_limit)
            results.append({"pdf": p, "status": "success", "meta": meta})
            print(json.dumps({"pdf": p, "pages": meta["pages"], "coverage": meta["coverage"],
                              "warnings": meta["warnings"]}, ensure_ascii=False))
        except Exception as exc:
            results.append({"pdf": p, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            print(json.dumps({"pdf": p, "status": "failed", "error": str(exc)}, ensure_ascii=False))
    os.makedirs(os.path.join(args.out, "structure"), exist_ok=True)
    with open(os.path.join(args.out, "structure", "parse_runs.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    return 0 if all(r["status"] == "success" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
