"""POST /search - image and/or barcode in, matching catalog products (with variant resolution) out.

Request JSON (one of image_base64, image_s3_key, barcode is required):
  image_base64 | image_s3_key   photo; s3 key must be under queries/
  barcode     optional; resolves the exact SKU (an image, if also sent, cross-checks it)
  size        optional hint, e.g. "XL" (wins over OCR)
  store_id    optional; enables per-store stock and stock-based size narrowing
  category, brand   optional exact-match filters for the image search
  top_k       optional, default 5 (max 20)
  detect_size optional bool, default true (OCR size tag/label from the image)
"""
import json
import time

from vsearch.config import services
from vsearch.http import response
from vsearch.pipeline import run_match


def handler(event, context):
    t0 = time.time()
    try:
        req = json.loads(event.get("body") or "{}")
        result = run_match(services(), req)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return response(400, {"error": str(e)})
    except LookupError as e:
        return response(404, {"error": str(e)})
    return response(200, {"request_id": context.aws_request_id, **result,
                          "latency_ms": int((time.time() - t0) * 1000)})
