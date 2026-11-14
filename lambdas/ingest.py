"""SQS-triggered catalog ingestion: upserts product + variant through the CatalogStore port and
indexes ONE embedding per (product, colour) image through the VectorStore port.

Message body (one variant per message):
{
  "product": {"product_id": "P100", "name": "...", "brand": "...", "category": "shirts",
              "description": "...", "price": 49.9, "currency": "USD", "url": "..."},
  "variant": {"sku": "P100-BLU-XL", "barcode": "...", "color": "Blue", "size": "XL",
              "price": 49.9, "stock": {"store1": 3}},
  "image_s3_key": "catalog/P100/blue.jpg"
}
Size variants that share an image share ONE vector (deduped by image hash), so the index scales
with styles x colours, not SKUs. Works with any adapter set (OpenSearch/Solr, DynamoDB/MySQL).
"""
import hashlib
import json
from decimal import Decimal

from vsearch.config import services

_ready = False


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "")}


def index_image(svc, product, color, key):
    img = svc.images.get(key)
    h = hashlib.sha256(img).hexdigest()
    meta = f"{product.get('category')}|{product.get('brand')}"
    reg = svc.catalog.get_registry(product["product_id"], color or "_")
    if reg and reg.get("image_hash") == h and reg.get("meta") == meta:
        return  # nothing changed (e.g. another size of the same colour, or a stock-only update)

    vec = svc.embedder.embed_image(img)
    fields = _clean({"product_id": product["product_id"], "color": color,
                     "category": product.get("category"), "brand": product.get("brand"),
                     "image_key": key})
    new_id = svc.vectors.add(vec, fields)
    svc.catalog.put_registry(product["product_id"], color or "_",
                             {"image_hash": h, "meta": meta, "doc_id": new_id})
    if reg and reg.get("doc_id"):  # replace: drop the stale vector
        try:
            svc.vectors.delete(reg["doc_id"])
        except Exception as e:
            print("stale doc delete failed:", reg["doc_id"], e)


def process(svc, msg):
    p, v = msg["product"], msg["variant"]
    color = (v.get("color") or "").strip().lower()
    svc.catalog.upsert_product(_clean({**p, "active": p.get("active", True)}))
    svc.catalog.upsert_variant(_clean({**v, "product_id": p["product_id"], "color": color}))
    if msg.get("image_s3_key"):
        index_image(svc, p, color, msg["image_s3_key"])


def handler(event, context):
    global _ready
    svc = services()
    if not _ready:
        svc.vectors.ensure_index(svc.embedder.dim)
        _ready = True
    failures = []
    for rec in event["Records"]:
        try:
            process(svc, json.loads(rec["body"], parse_float=Decimal))
        except Exception as e:
            print("ingest failed", rec["messageId"], repr(e))
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
