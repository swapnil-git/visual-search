"""Shared engine: image and/or barcode -> candidates -> products and variants -> resolved variants.

Every use case (product search, inventory check, ...) is a thin handler around run_match().
Nothing here knows which vector store, database, embedding model or OCR service is in use.
"""
import base64
import binascii
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .matching import (MIN_SCORE, best_per_product, confidence, norm_size,
                       resolve_variant, size_from_lines)
from .ports import (CatalogStore, Embedder, ImageStore, InventoryProvider,
                    TextReader, VectorStore)

KNN_CANDIDATES = int(os.environ.get("KNN_CANDIDATES", "50"))
ENABLE_OCR = os.environ.get("ENABLE_OCR", "true").lower() == "true"
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass
class Services:
    embedder: Embedder
    vectors: VectorStore
    catalog: CatalogStore
    inventory: InventoryProvider
    images: Optional[ImageStore] = None
    ocr: Optional[TextReader] = None


def load_image(svc: Services, req: dict) -> bytes:
    if req.get("image_base64"):
        raw = req["image_base64"]
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            img = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image_base64 is not valid base64")
    elif req.get("image_s3_key"):
        key = req["image_s3_key"]
        if svc.images is None:
            raise ValueError("image_s3_key is not supported by this deployment")
        if not key.startswith("queries/") or ".." in key:
            raise ValueError("image_s3_key must be under queries/")
        img = svc.images.get(key)
    else:
        raise ValueError("provide image_base64 or image_s3_key")
    if not img or len(img) > MAX_IMAGE_BYTES:
        raise ValueError("image missing or larger than 5 MB (downscale to ~1024px JPEG)")
    return img


def _read_size(svc, img):
    try:
        return size_from_lines(svc.ocr.read_lines(img))
    except Exception as e:  # OCR is best-effort
        print("ocr failed:", e)
        return None


def run_match(svc: Services, req: dict) -> dict:
    """Returns {"detected": {...}, "matches": [...]}.
    Raises ValueError for bad input and LookupError when only a barcode was sent and it is unknown."""
    barcode = str(req.get("barcode") or "").strip() or None
    has_image = bool(req.get("image_base64") or req.get("image_s3_key"))
    if not has_image and not barcode:
        raise ValueError("provide image_base64, image_s3_key or barcode")
    img = load_image(svc, req) if has_image else None
    top_k = max(1, min(int(req.get("top_k", 5)), 20))

    store_id = str(req["store_id"]) if req.get("store_id") else None
    size = norm_size(req.get("size"))
    size_source = "request" if size else "none"

    cands = []
    if img:
        want_ocr = svc.ocr is not None and ENABLE_OCR and not size and req.get("detect_size", True)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_vec = ex.submit(svc.embedder.embed_image, img)
            f_ocr = ex.submit(_read_size, svc, img) if want_ocr else None
            vec = f_vec.result()
            if f_ocr:
                size = f_ocr.result()
                size_source = "ocr" if size else "none"
        filters = {k: req[k] for k in ("category", "brand") if req.get(k)}
        hits = svc.vectors.knn(vec, KNN_CANDIDATES, filters)
        cands = [c for c in best_per_product(hits) if c["score"] >= MIN_SCORE]

    barcode_found = None
    if barcode:
        bv = svc.catalog.find_variant_by_barcode(barcode)
        barcode_found = bv is not None
        if bv:
            seen = next((c for c in cands if c["product_id"] == bv["product_id"]), None)
            cands = [c for c in cands if c["product_id"] != bv["product_id"]]
            cands.insert(0, {"product_id": bv["product_id"], "color": bv.get("color"),
                             "barcode": True, "barcode_sku": bv["sku"], "barcode_size": bv.get("size"),
                             "score": seen["score"] if seen else None,
                             "image_agrees": (seen is not None) if img else None})
        elif not img:
            raise LookupError("barcode not found")

    products = svc.catalog.get_products([c["product_id"] for c in cands])
    cands = [c for c in cands if c["product_id"] in products
             and products[c["product_id"]].get("active", True)][:top_k]
    pids = [c["product_id"] for c in cands]
    variants_by_pid = svc.catalog.get_variants_many(pids) if pids else {}

    all_v = [v for vs in variants_by_pid.values() for v in vs]
    stock = svc.inventory.get_stock(all_v, [store_id] if store_id else None) if all_v else {}
    variants_by_pid = {pid: [{**v, "stock": stock[v["sku"]]} if v.get("sku") in stock else v for v in vs]
                       for pid, vs in variants_by_pid.items()}

    matches = []
    for i, c in enumerate(cands):
        nxt = cands[i + 1]["score"] if i + 1 < len(cands) else None
        if c.get("barcode"):
            sz, src = norm_size(c["barcode_size"]), "barcode"
        else:
            sz, src = size, size_source
        res, pool = resolve_variant(variants_by_pid[c["product_id"]], c["color"], sz, src, store_id)
        if c.get("barcode"):
            hit = next((v for v in pool if v.get("sku") == c["barcode_sku"]), None)
            if hit:
                res["matched_variant"], res["status"] = hit, "exact"
            conf = "medium" if c.get("image_agrees") is False else "high"
        else:
            conf = confidence(c["score"], nxt)
        matches.append({"rank": i + 1, "score": c["score"], "confidence": conf,
                        "matched_by": "barcode" if c.get("barcode") else "image",
                        "image_agrees": c.get("image_agrees"),
                        "product": products[c["product_id"]],
                        "variant_resolution": res,
                        "variants": pool})

    return {"detected": {"size": size, "size_source": size_source, "barcode_found": barcode_found},
            "matches": matches}
