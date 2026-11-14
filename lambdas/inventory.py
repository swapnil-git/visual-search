"""POST /inventory/check - "do we have this item, in what sizes, and where else?"

Same engine as /search (photo and/or barcode, plus optional size), but the answer is stock:
for each match, quantity of the resolved SKU at `store_id`, quantity per size, and other stores that
hold the SKU. A second use case on the shared pipeline: add more the same way.

Request: as /search, with store_id REQUIRED. Optional max_other_stores (default 5).
"""
import json
import time

from vsearch.config import services
from vsearch.http import response
from vsearch.matching import availability
from vsearch.pipeline import run_match


def handler(event, context):
    t0 = time.time()
    try:
        req = json.loads(event.get("body") or "{}")
        if not req.get("store_id"):
            raise ValueError("store_id is required")
        max_other = max(0, min(int(req.get("max_other_stores", 5)), 20))
        result = run_match(services(), req)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return response(400, {"error": str(e)})
    except LookupError as e:
        return response(404, {"error": str(e)})

    items = []
    for m in result["matches"]:
        vr = m["variant_resolution"]
        items.append({"rank": m["rank"], "score": m["score"], "confidence": m["confidence"],
                      "matched_by": m["matched_by"],
                      "product": {k: m["product"].get(k) for k in ("product_id", "name", "brand", "url")},
                      "status": vr["status"], "color": vr["color"], "size": vr["size"],
                      "availability": availability(m["variants"], vr["matched_variant"],
                                                  str(req["store_id"]), max_other)})
    return response(200, {"request_id": context.aws_request_id, "store_id": str(req["store_id"]),
                          "detected": result["detected"], "items": items,
                          "latency_ms": int((time.time() - t0) * 1000)})
