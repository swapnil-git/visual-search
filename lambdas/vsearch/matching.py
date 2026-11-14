"""Pure logic: sizes, stock, variant resolution, scoring. No I/O, so it is unit-testable offline."""
import os
import re
from decimal import Decimal

MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.55"))
HIGH_SCORE = float(os.environ.get("HIGH_SCORE", "0.80"))

_SIZE_MAP = {"XSMALL": "XS", "EXTRASMALL": "XS", "SMALL": "S", "MEDIUM": "M",
             "LARGE": "L", "XLARGE": "XL", "EXTRALARGE": "XL", "2XL": "XXL",
             "XXLARGE": "XXL", "3XL": "XXXL"}
ALPHA_SIZES = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def norm_size(s):
    if s is None:
        return None
    t = re.sub(r"[\s\-_/]", "", str(s).upper())
    return _SIZE_MAP.get(t, t) or None


def size_sort_key(s):
    return (0, ALPHA_SIZES.index(s)) if s in ALPHA_SIZES else (1, str(s))


def size_from_lines(lines, min_conf=85):
    """Return a size only if exactly one clean size token is read (tag/label in frame)."""
    found = set()
    for text, conf in lines:
        if conf < min_conf:
            continue
        t = norm_size(text)
        if t in ALPHA_SIZES:
            found.add(t)
    return found.pop() if len(found) == 1 else None


def best_per_product(hits):
    """Collapse image hits (Hit objects) to the best-scoring one per product."""
    seen = {}
    for h in sorted(hits, key=lambda h: -h.score):
        pid = h.fields["product_id"]
        if pid not in seen:
            seen[pid] = {"product_id": pid, "color": h.fields.get("color"),
                         "image_key": h.fields.get("image_key"), "score": round(h.score, 4)}
    return list(seen.values())


def stock_qty(variant, store_id):
    """stock may be a number (global) or a map {store_id: qty}. None = unknown."""
    s = variant.get("stock")
    if isinstance(s, dict):
        if store_id:
            s = s.get(store_id)
        else:
            s = sum(x for x in s.values() if isinstance(x, (int, float, Decimal)))
    if isinstance(s, (int, float, Decimal)) and not isinstance(s, bool):
        return s
    return None


def annotate(v, store_id):
    q = stock_qty(v, store_id)
    return {**v, "stock_qty": q, "in_stock": (q > 0) if q is not None else None}


def resolve_variant(variants, color, size, size_source, store_id=None):
    """Colour comes from the visually matched image; size from barcode / hint / OCR / stock.

    With a store_id and no size, a single in-stock size is returned as `inferred_by_stock`
    (a suggestion, not a certainty)."""
    color_l = (color or "").lower()
    in_color = [v for v in variants if color_l and str(v.get("color", "")).lower() == color_l]
    pool = [annotate(v, store_id) for v in (in_color or variants)]
    sized = [v for v in pool if v.get("size")]
    sizes = sorted({norm_size(v["size"]) for v in sized}, key=size_sort_key)
    in_stock_sizes = sorted({norm_size(v["size"]) for v in sized if v["in_stock"] is not False},
                            key=size_sort_key)
    matched, status = None, "no_variants"
    if pool:
        if size:
            hit = [v for v in pool if norm_size(v.get("size")) == size]
            hit.sort(key=lambda v: v["in_stock"] is False)  # prefer a SKU that is in stock
            matched, status = (hit[0], "exact") if hit else (None, "size_not_available")
        elif len(sizes) <= 1:
            matched, status = pool[0], "exact"
        elif store_id and len(in_stock_sizes) == 1:
            matched = next(v for v in sized if norm_size(v["size"]) == in_stock_sizes[0]
                           and v["in_stock"] is not False)
            status, size_source = "inferred_by_stock", "stock"
        else:
            status = "size_required"  # style + colour known, size ambiguous
    if matched is not None and not size:
        size = norm_size(matched.get("size"))
    return {"status": status, "color": color, "size": size,
            "size_source": size_source, "matched_variant": matched,
            "available_sizes": sizes,
            "in_stock_sizes": in_stock_sizes if store_id else None}, pool


def confidence(score, next_score):
    if score >= HIGH_SCORE and (next_score is None or score - next_score >= 0.03):
        return "high"
    return "medium" if score >= MIN_SCORE else "low"


def availability(pool, matched, store_id, max_other=5):
    """Inventory view for one match: every size at this store, plus other stores that stock the SKU."""
    rows = sorted(({"sku": v.get("sku"), "size": norm_size(v.get("size")),
                    "qty": v.get("stock_qty"), "in_stock": v.get("in_stock")} for v in pool),
                  key=lambda r: size_sort_key(r["size"]))
    out = {"store_id": store_id, "sizes": rows}
    if matched is not None:
        out.update({"matched_sku": matched.get("sku"), "qty": matched.get("stock_qty"),
                    "in_stock": matched.get("in_stock")})
        s = matched.get("stock")
        if isinstance(s, dict) and max_other > 0:
            others = [{"store_id": k, "qty": q} for k, q in s.items()
                      if k != store_id and isinstance(q, (int, float, Decimal)) and q > 0]
            out["other_stores"] = sorted(others, key=lambda o: -o["qty"])[:max_other]
    return out
