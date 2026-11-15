#!/usr/bin/env python3
"""Run the whole API on your laptop with no AWS: in-memory adapters + the real handlers.

  pip install -r scripts/requirements-dev.txt
  python scripts/local_server.py --data sample-data --port 8080
  python scripts/e2e_test.py --base http://localhost:8080/v1 --data sample-data --skip ocr

It loads catalog.csv through the real ingest code (same path as the SQS-fed Lambda), then serves
POST /v1/search and POST /v1/inventory/check. The API key header is accepted and ignored.
The local embedder is a crude pixel stand-in: this checks the plumbing, not real-world accuracy.
"""
import argparse
import csv
import importlib.util
import json
import os
import sys
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambdas"))


def load_enqueue():
    spec = importlib.util.spec_from_file_location("enqueue_catalog", os.path.join(HERE, "enqueue_catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="sample-data", help="feed folder (default: the committed sample-data/)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--min-score", default="0.5", help="MIN_SCORE for the crude local embedder (default 0.5)")
    a = ap.parse_args()

    os.environ.update({"EMBEDDER": "local", "VECTOR_STORE": "local", "CATALOG_STORE": "local",
                       "IMAGE_STORE": "local", "OCR": "none", "INVENTORY": "embedded",
                       "LOCAL_IMAGE_ROOT": os.path.join(a.data, "catalog_images")})
    os.environ.setdefault("MIN_SCORE", a.min_score)
    os.environ.setdefault("HIGH_SCORE", "0.8")

    import ingest, inventory, search  # noqa: E402  (after env is set)
    from vsearch.config import services

    svc = services()
    svc.vectors.ensure_index(svc.embedder.dim)
    to_msg = load_enqueue().row_to_msg
    n = 0
    with open(os.path.join(a.data, "catalog.csv"), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ingest.process(svc, to_msg(row))
            n += 1
    print(f"loaded {n} variants, {len(svc.catalog.products)} products, {len(svc.vectors._docs)} image vectors")

    routes = {"/v1/search": search.handler, "/v1/inventory/check": inventory.handler}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            fn = routes.get(self.path.rstrip("/"))
            if fn is None:
                return self._send(404, {"message": "not found"})
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            r = fn({"body": body}, types.SimpleNamespace(aws_request_id=uuid.uuid4().hex))
            self._send(r["statusCode"], None, r["body"])

        def _send(self, code, obj=None, raw=None):
            data = (raw if raw is not None else json.dumps(obj)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    print(f"serving on http://localhost:{a.port}/v1  (search, inventory/check). Ctrl-C to stop.", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
