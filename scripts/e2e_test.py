#!/usr/bin/env python3
"""End-to-end test: send the labelled queries in <data>/queries.json to a running API and score the answers.

  # against a deployed stack
  python scripts/e2e_test.py --base https://<id>.execute-api.<region>.amazonaws.com/v1 --key <API_KEY> --data sample-data
  # against scripts/local_server.py
  python scripts/e2e_test.py --base http://localhost:8080/v1 --data sample-data --skip ocr

Standard library only. Prints one line per query, a summary by kind, and score statistics to help you set
MIN_SCORE / HIGH_SCORE. Exit code 0 = all hard checks passed, 1 = something failed, 2 = could not reach the API.
Hard checks: image retrieval (visual, visual_size) must reach --min-pass (default 0.85); barcode, stock_inferred and
inventory must all pass. 'ocr' and 'negative' are soft: reported, never fail the run.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HARD_ALL = {"barcode", "stock_inferred", "inventory"}
HARD_RATE = {"visual", "visual_size"}


def call(base, endpoint, key, body, retries=3):
    path = {"search": "/search", "inventory": "/inventory/check"}[endpoint]
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.loads(r.read()), int((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            try:
                return e.code, json.loads(raw), int((time.time() - t0) * 1000)
            except ValueError:
                return e.code, {"error": raw[:200]}, int((time.time() - t0) * 1000)
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"cannot reach {base}: {e}") from None


def build_body(q, data_dir):
    body = dict(q["request"])
    if q.get("file"):
        with open(os.path.join(data_dir, q["file"]), "rb") as f:
            body["image_base64"] = base64.b64encode(f.read()).decode()
    return body


def check(q, status, resp):
    """-> (ok, detail, top1 score or None)"""
    kind, ex = q["kind"], q["expect"]
    if status == 403:
        raise SystemExit("HTTP 403: API key missing or wrong (use --key).")
    if kind == "inventory":
        if status != 200 or not resp.get("items"):
            return False, f"http {status}, no items", None
        it = resp["items"][0]
        av = it["availability"]
        others = [o["store_id"] for o in av.get("other_stores", [])]
        ok = (it["product"]["product_id"] == ex["product_id"] and av.get("matched_sku") == ex["sku"]
              and av.get("in_stock") == ex["in_stock"] and ex["other_store"] in others)
        return ok, f"sku={av.get('matched_sku')} in_stock={av.get('in_stock')} other_stores={others}", it["score"]
    if kind == "negative":
        ms = resp.get("matches", []) if status == 200 else []
        high = [m for m in ms if m["confidence"] == "high"]
        top = ms[0]["score"] if ms else None
        return not high, f"matches={len(ms)} high_confidence={len(high)}", top
    if status != 200 or not resp.get("matches"):
        return False, f"http {status}, no matches", None
    m = resp["matches"][0]
    vr = m["variant_resolution"]
    mv = vr.get("matched_variant") or {}
    got = m["product"]["product_id"]
    if kind == "barcode":
        return (mv.get("sku") == ex["sku"] and m["matched_by"] == "barcode"), f"sku={mv.get('sku')}", None
    if kind == "ocr":
        det = resp["detected"]["size"]
        return det == ex["size"], f"detected size={det!r} product={got}", m["score"]
    if kind == "stock_inferred":
        ok = got == ex["product_id"] and vr["status"] == ex["status"] and mv.get("sku") == ex["sku"]
        return ok, f"product={got} status={vr['status']} sku={mv.get('sku')}", m["score"]
    ranks = [x["product"]["product_id"] for x in resp["matches"]]
    ok = got == ex["product_id"] and (vr["color"] or "").lower() == ex["color"] and vr["status"] == ex["status"]
    if "sku" in ex:
        ok = ok and mv.get("sku") == ex["sku"]
    top3 = ex["product_id"] in ranks[:3]
    return ok, f"top1={got} color={vr['color']} status={vr['status']}" + ("" if got == ex["product_id"] else f" (in top3: {top3})"), m["score"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="API base including the stage, e.g. https://x.execute-api.us-east-1.amazonaws.com/v1")
    ap.add_argument("--key", default=os.environ.get("API_KEY"), help="x-api-key (or env API_KEY)")
    ap.add_argument("--data", default="sample-data")
    ap.add_argument("--min-pass", type=float, default=0.85, help="required pass rate for visual and visual_size")
    ap.add_argument("--skip", default="", help="comma list of kinds to skip, e.g. ocr,negative")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the catalog to become searchable first")
    ap.add_argument("-v", "--verbose", action="store_true", help="print passing queries too")
    a = ap.parse_args()

    with open(os.path.join(a.data, "queries.json")) as f:
        queries = [q for q in json.load(f)["queries"] if q["kind"] not in a.skip.split(",")]

    if a.wait:  # ingestion is asynchronous: wait until a known catalog image is found
        probe = next(q for q in queries if q["kind"] == "visual")
        end = time.time() + a.wait
        while True:
            st, r, _ = call(a.base, "search", a.key, build_body(probe, a.data))
            if st == 200 and r.get("matches"):
                break
            if time.time() > end:
                raise SystemExit(f"catalog not searchable after {a.wait}s (last status {st}). Is ingestion finished? Check the DLQ.")
            print("waiting for catalog to be indexed...", flush=True)
            time.sleep(10)

    by_kind, scores_ok, scores_bad, lat = {}, [], [], []
    print(f"{'id':9} {'kind':15} {'result':10} {'ms':>5}  detail")
    for q in queries:
        st, resp, ms = call(a.base, q["endpoint"], a.key, build_body(q, a.data))
        ok, detail, score = check(q, st, resp)
        lat.append(ms)
        res = "PASS" if ok else ("soft-fail" if q["soft"] else "FAIL")
        by_kind.setdefault(q["kind"], []).append(ok)
        if score is not None:
            (scores_ok if ok else scores_bad).append((q["kind"], score))
        if a.verbose or not ok:
            print(f"{q['id']:9} {q['kind']:15} {res:10} {ms:5}  {detail}")

    print("\nsummary")
    failed = False
    for kind, oks in by_kind.items():
        rate = sum(oks) / len(oks)
        if kind in HARD_ALL:
            verdict, bad = ("ok", False) if all(oks) else ("FAIL (must be 100%)", True)
        elif kind in HARD_RATE:
            verdict, bad = ("ok", False) if rate >= a.min_pass else (f"FAIL (need {a.min_pass:.0%})", True)
        else:
            verdict, bad = "informational", False
        failed |= bad
        print(f"  {kind:15} {sum(oks)}/{len(oks)}  {rate:5.0%}  {verdict}")
    lat.sort()
    print(f"  latency ms: median {lat[len(lat) // 2]}, p95 {lat[min(len(lat) - 1, int(len(lat) * 0.95))]}, max {lat[-1]}")
    good = [s for k, s in scores_ok if k in HARD_RATE | {"stock_inferred"}]
    bad = [s for k, s in scores_bad if k in HARD_RATE | {"stock_inferred"}]
    neg = [s for k, s in scores_ok + scores_bad if k == "negative"]
    if good:
        print(f"  top-1 score, correct matches : min {min(good):.2f}  median {sorted(good)[len(good) // 2]:.2f}")
    if bad:
        print(f"  top-1 score, wrong matches   : max {max(bad):.2f}")
    if neg:
        print(f"  top-1 score, non-garment     : max {max(neg):.2f}")
    if good and neg:
        if max(neg) < min(good):
            print(f"  MIN_SCORE guidance: non-garment images score up to {max(neg):.2f}, correct matches from {min(good):.2f}:"
                  f" a MIN_SCORE between them (API default 0.55) rejects unrelated photos without losing real matches.")
        else:
            print(f"  MIN_SCORE guidance: non-garment images score up to {max(neg):.2f} but some correct matches score only {min(good):.2f}:"
                  f" a threshold alone cannot separate them. Try better or more catalog images per colour, or a different embedding model.")
    if good and bad:
        print(f"  Wrong matches reach {max(bad):.2f}, so HIGH_SCORE (the 'high' confidence cut-off, default 0.80) should sit above that"
              f" if you want 'high' to mean reliable; the closer the gap to the runner-up, the more trustworthy a match is.")
    print("RESULT:", "FAILED" if failed else "PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
