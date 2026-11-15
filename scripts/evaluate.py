#!/usr/bin/env python3
"""Measures the deployed search API against a sample folder made by make_sample_catalog.py.
Standard library only.

  API_URL=https://.../v1/search API_KEY=... python scripts/evaluate.py sample
  ... python scripts/evaluate.py sample --no-size      # also checks that size is NOT guessed (size_required)
  ... python scripts/evaluate.py sample --min-top1 0.9 # exit code 1 if top-1 product accuracy is below 90%

Reports: top-1 / top-k product accuracy, colour accuracy, exact-variant (SKU) accuracy when size is sent,
the confidence label of correct vs wrong top-1 answers (to tune MIN_SCORE / HIGH_SCORE), and latency.
Run it after ingestion finishes (the SQS queue is empty and the dead-letter queue has no messages).
"""
import argparse
import base64
import csv
import http.client
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request


def call(url, key, payload, retries=2):
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1 + attempt)
                continue
            return e.code, {"error": e.read().decode(errors="replace")[:200]}
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            return 0, {"error": str(e)}


def pct(n, d):
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


def evaluate(folder, url, key, send_size=True, top_k=5, quiet=False):
    with open(os.path.join(folder, "expected.csv"), newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    s = {"n": 0, "errors": 0, "top1": 0, "topk": 0, "color": 0, "sku": 0, "size_req": 0}
    conf = {"right": {}, "wrong": {}}
    lat, fails = [], []
    for c in cases:
        with open(os.path.join(folder, c["query_file"]), "rb") as f:
            payload = {"image_base64": base64.b64encode(f.read()).decode(), "top_k": top_k}
        if send_size:
            payload["size"] = c["size"]
        t0 = time.time()
        status, data = call(url, key, payload)
        lat.append((time.time() - t0) * 1000)
        s["n"] += 1
        if status != 200:
            s["errors"] += 1
            fails.append(f"{c['query_file']}: HTTP {status} {data.get('error', '')}")
            continue
        matches = data.get("matches", [])
        ids = [m["product"]["product_id"] for m in matches]
        ok1 = bool(ids) and ids[0] == c["product_id"]
        s["top1"] += ok1
        s["topk"] += c["product_id"] in ids
        if ok1:
            vr = matches[0]["variant_resolution"]
            col_ok = (vr.get("color") or "").lower() == c["color"]
            s["color"] += col_ok
            if send_size:
                sku_ok = (vr.get("matched_variant") or {}).get("sku") == c["sku"]
                s["sku"] += sku_ok
                if not (col_ok and sku_ok):
                    fails.append(f"{c['query_file']}: right product, variant {vr['status']} "
                                 f"{(vr.get('matched_variant') or {}).get('sku')} (wanted {c['sku']})")
            else:
                s["size_req"] += vr["status"] == "size_required"
        else:
            fails.append(f"{c['query_file']}: wanted {c['product_id']}, got {ids[:3] or 'no match'}")
        if matches:
            bucket = conf["right" if ok1 else "wrong"]
            bucket[matches[0]["confidence"]] = bucket.get(matches[0]["confidence"], 0) + 1
    s["lat"], s["fails"], s["conf"] = lat, fails, conf
    if not quiet:
        report(s, send_size, top_k)
    return s


def report(s, send_size, top_k):
    n, ok = s["n"], s["n"] - s["errors"]
    print(f"queries: {n}   errors: {s['errors']}")
    print(f"top-1 product accuracy:     {pct(s['top1'], n)}")
    print(f"top-{top_k} product accuracy:     {pct(s['topk'], n)}")
    print(f"colour correct (given top-1 correct): {pct(s['color'], s['top1'])}")
    if send_size:
        print(f"exact SKU (size sent):      {pct(s['sku'], s['top1'])} of correct top-1")
    else:
        print(f"size_required (no size sent): {pct(s['size_req'], s['top1'])} of correct top-1")
    print(f"confidence when top-1 right: {s['conf']['right']}   when wrong: {s['conf']['wrong']}")
    if s["lat"]:
        q = sorted(s["lat"])
        print(f"latency ms (client side): p50 {statistics.median(q):.0f}   p95 {q[int(0.95 * (len(q) - 1))]:.0f}")
    for line in s["fails"][:10]:
        print("  miss:", line)
    if len(s["fails"]) > 10:
        print(f"  ... and {len(s['fails']) - 10} more")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="folder made by make_sample_catalog.py (needs expected.csv and queries/)")
    p.add_argument("--no-size", action="store_true", help="do not send the size hint")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-top1", type=float, default=0.0, help="fail (exit 1) below this top-1 accuracy (0-1)")
    a = p.parse_args()
    url, key = os.environ.get("API_URL"), os.environ.get("API_KEY")
    if not url or not key:
        sys.exit("set API_URL (the SearchUrl output) and API_KEY")
    s = evaluate(a.folder, url, key, not a.no_size, a.top_k)
    sys.exit(1 if s["n"] == 0 or s["top1"] / s["n"] < a.min_top1 or s["errors"] == s["n"] else 0)


if __name__ == "__main__":
    main()
