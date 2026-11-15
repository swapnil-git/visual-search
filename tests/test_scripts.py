"""Tests for scripts/make_sample_catalog.py and scripts/evaluate.py (no AWS, no network beyond localhost).
Run: python3 tests/test_scripts.py"""
import base64
import csv
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import enqueue_catalog  # noqa: E402
import evaluate  # noqa: E402
import make_sample_catalog as gen  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    out = os.path.join(tmp, "sample")
    sys.argv = ["x", "synthetic", "--out", out, "--products", "6", "--colors", "2", "--queries", "6"]
    gen.main()

    # --- feed shape
    rows = list(csv.DictReader(open(os.path.join(out, "catalog.csv"), encoding="utf-8")))
    assert len(rows) == 6 * 2 * len(gen.SIZES) and set(rows[0]) == set(gen.COLUMNS)
    assert len({r["sku"] for r in rows}) == len(rows) and len({r["barcode"] for r in rows}) == len(rows)
    for r in rows:
        assert os.path.exists(os.path.join(out, "images", *r["image_s3_key"].split("/")[1:]))
    # the same colour shares one image across sizes (so the index gets one vector per product+colour)
    assert len({(r["product_id"], r["color"], r["image_s3_key"]) for r in rows}) == 6 * 2
    # rows convert to valid ingest messages (stock map parsed, price numeric)
    msg = enqueue_catalog.row_to_msg(rows[0])
    assert isinstance(msg["variant"]["stock"], dict) and isinstance(msg["variant"]["price"], float)
    assert msg["product"]["product_id"] == rows[0]["product_id"] and msg["image_s3_key"].startswith("catalog/")
    # images are real PNGs, and query photos differ from the catalog photo of the same item
    png = open(os.path.join(out, "images", rows[0]["product_id"], gen.slug(rows[0]["color"]) + ".png"), "rb").read()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    exp = list(csv.DictReader(open(os.path.join(out, "expected.csv"), encoding="utf-8")))
    assert len(exp) == 6
    for e in exp:
        q = open(os.path.join(out, e["query_file"]), "rb").read()
        cat = open(os.path.join(out, "images", e["product_id"], gen.slug(e["color"]) + ".png"), "rb").read()
        assert q != cat
        assert any(r["sku"] == e["sku"] for r in rows)

    # --- evaluator against a mock API that answers from the ground truth, with one deliberate miss
    truth = {hashlib.sha256(open(os.path.join(out, e["query_file"]), "rb").read()).hexdigest(): e for e in exp}
    wrong = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers["x-api-key"] != "k":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"message":"Forbidden"}')
                return
            e = truth[hashlib.sha256(base64.b64decode(req["image_base64"])).hexdigest()]
            wrong["n"] += 1
            pid = "SYN999" if wrong["n"] == 1 else e["product_id"]
            vr = ({"status": "exact", "color": e["color"], "matched_variant": {"sku": e["sku"]}} if "size" in req
                  else {"status": "size_required", "color": e["color"], "matched_variant": None})
            body = json.dumps({"matches": [{"product": {"product_id": pid}, "variant_resolution": vr,
                                            "confidence": "high"}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/v1/search"

    s = evaluate.evaluate(out, url, "k", send_size=True, quiet=True)
    assert s["n"] == 6 and s["errors"] == 0 and s["top1"] == 5 and s["sku"] == 5 and s["color"] == 5
    assert s["conf"]["wrong"] == {"high": 1} and any("SYN999" in f for f in s["fails"])
    wrong["n"] = 99
    s = evaluate.evaluate(out, url, "k", send_size=False, quiet=True)
    assert s["top1"] == 6 and s["size_req"] == 6
    s = evaluate.evaluate(out, url, "wrong-key", send_size=True, quiet=True)  # 403s are counted, not crashes
    assert s["errors"] == 6 and s["top1"] == 0 and any("HTTP 403" in f for f in s["fails"])
    s = evaluate.evaluate(out, "http://127.0.0.1:1/v1/search", "k", send_size=True, quiet=True)  # nothing listening
    assert s["errors"] == 6
    srv.shutdown()

print("script tests passed")

# --- kaggle-format conversion with a fake styles.csv (incl. an unquoted comma in a name, a non-apparel row,
#     a row with no image and a row with no colour)
with tempfile.TemporaryDirectory() as tmp:
    imgs = os.path.join(tmp, "images")
    os.makedirs(imgs)
    hdr = "id,gender,masterCategory,subCategory,articleType,baseColour,season,year,usage,productDisplayName\n"
    lines = ["1,Men,Apparel,Topwear,Shirts,Navy Blue,Fall,2011,Casual,Arrow Men Blue Shirt",
             "2,Men,Apparel,Topwear,Tshirts,Black,Fall,2011,Casual,Puma, Men Black Tee",  # comma in name
             "3,Women,Accessories,Bags,Handbags,Brown,Fall,2011,Casual,Brown Bag",
             "4,Women,Apparel,Topwear,Tops,Red,Fall,2011,Casual,Missing image",
             "5,Women,Apparel,Topwear,Tops,,Fall,2011,Casual,No colour",
             "6,Women,Apparel,Dress,Dresses,Green,Fall,2011,Casual,Green Dress"]
    open(os.path.join(tmp, "styles.csv"), "w").write(hdr + "\n".join(lines) + "\n")
    for i in (1, 2, 3, 5, 6):
        open(os.path.join(imgs, f"{i}.jpg"), "wb").write(b"\xff\xd8fake-jpeg-" + str(i).encode())
    out = os.path.join(tmp, "k")
    sys.argv = ["x", "kaggle", "--styles", os.path.join(tmp, "styles.csv"), "--images-dir", imgs, "--out", out,
                "--limit", "10", "--queries", "2"]
    gen.main()
    rows = list(csv.DictReader(open(os.path.join(out, "catalog.csv"), encoding="utf-8")))
    assert {r["product_id"] for r in rows} == {"K1", "K2", "K6"}          # only valid apparel with image + colour
    assert next(r for r in rows if r["product_id"] == "K2")["name"] == "Puma, Men Black Tee"
    assert next(r for r in rows if r["product_id"] == "K1")["color"] == "Navy Blue"
    assert os.path.exists(os.path.join(out, "images", "K1", "navy-blue.jpg"))
    assert len(list(csv.DictReader(open(os.path.join(out, "expected.csv"))))) == 2
    sys.argv[-4:] = ["--limit", "10", "--queries", "2", "--article-types", "Dresses"]
    gen.main()
    assert {r["product_id"] for r in csv.DictReader(open(os.path.join(out, "catalog.csv")))} == {"K6"}

print("kaggle conversion tests passed")
