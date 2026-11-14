"""Offline tests: no AWS, no network. Run: python3 tests/test_logic.py

The core is exercised end to end with in-memory fake adapters, which also shows how a new backend plugs in.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas"))

from vsearch import config, matching  # noqa: E402
from vsearch.adapters import mysql as mysql_mod, solr as solr_mod  # noqa: E402
from vsearch.adapters.opensearch import _l2_to_cosine  # noqa: E402
from vsearch.pipeline import Services, run_match  # noqa: E402
from vsearch.ports import (CatalogStore, Embedder, Hit, InventoryProvider,  # noqa: E402
                           TextReader, VectorStore)

# ------------------------------------------------------------------ pure logic
V = [{"sku": "S-BLU-M", "color": "blue", "size": "M"},
     {"sku": "S-BLU-XL", "color": "blue", "size": "XL"},
     {"sku": "S-RED-XL", "color": "red", "size": "XL"}]
assert matching.norm_size("x-large") == "XL" and matching.norm_size("2xl") == "XXL"
assert matching.size_from_lines([("XL", 99), ("SALE", 99)]) == "XL"
assert matching.size_from_lines([("XL", 99), ("M", 99)]) is None  # ambiguous -> no guess
assert matching.size_from_lines([("XL", 50)]) is None             # low confidence
r, _ = matching.resolve_variant(V, "Blue", "XL", "ocr")
assert r["status"] == "exact" and r["matched_variant"]["sku"] == "S-BLU-XL"
r, _ = matching.resolve_variant(V, "blue", None, "none")
assert r["status"] == "size_required" and r["available_sizes"] == ["M", "XL"]
r, _ = matching.resolve_variant(V, "red", "M", "request")
assert r["status"] == "size_not_available"
assert matching.confidence(0.9, 0.6) == "high" and matching.confidence(0.9, 0.89) == "medium"

S = [{"sku": "T-M", "color": "blue", "size": "M", "stock": {"st1": 0, "st2": 4}},
     {"sku": "T-XL", "color": "blue", "size": "XL", "stock": {"st1": 3, "st2": 0}}]
r, _ = matching.resolve_variant(S, "blue", None, "none", "st1")
assert r["status"] == "inferred_by_stock" and r["matched_variant"]["sku"] == "T-XL" and r["size"] == "XL"
r, _ = matching.resolve_variant(S, "blue", None, "none", None)
assert r["status"] == "size_required" and r["in_stock_sizes"] is None
r, pool = matching.resolve_variant(S, "blue", "M", "request", "st1")  # asked size out of stock here
assert r["status"] == "exact" and r["matched_variant"]["in_stock"] is False
av = matching.availability(pool, r["matched_variant"], "st1")
assert av["in_stock"] is False and av["other_stores"] == [{"store_id": "st2", "qty": 4}]
assert [x["size"] for x in av["sizes"]] == ["M", "XL"]
assert matching.stock_qty({"stock": {"a": 2, "b": 3}}, None) == 5
assert matching.stock_qty({"stock": {"a": 2}}, "zz") is None

# score conversions: identical -> 1, orthogonal unit vectors -> 0
assert abs(_l2_to_cosine(1.0) - 1.0) < 1e-9 and abs(_l2_to_cosine(1 / 3) - 0.0) < 1e-9
assert abs(solr_mod._cosine(1.0) - 1.0) < 1e-9 and abs(solr_mod._cosine(0.5) - 0.0) < 1e-9

# MySQL row mapping + upsert SQL (no DB needed)
p = mysql_mod._row_to_product({"product_id": "P1", "name": "Shirt", "active": 1, "attributes": '{"material": "cotton"}'})
assert p == {"material": "cotton", "product_id": "P1", "name": "Shirt", "active": True}
v = mysql_mod._row_to_variant({"sku": "A", "stock": '{"st1": 2}', "attributes": None})
assert v["stock"] == {"st1": 2}
sql = mysql_mod._upsert_sql("variants", ["product_id", "sku", "size"], {"product_id", "sku"})
assert "ON DUPLICATE KEY UPDATE size=VALUES(size)" in sql and "product_id=VALUES" not in sql


# ------------------------------------------------------------------ in-memory fakes
class FakeEmbedder(Embedder):
    dim = 2

    def embed_image(self, image):
        return [1.0, 0.0]


class FakeVectors(VectorStore):
    def ensure_index(self, dim): pass
    def add(self, vector, fields): return "d1"
    def delete(self, doc_id): pass

    def knn(self, vector, k, filters):
        hits = [Hit("d1", 0.93, {"product_id": "P1", "color": "blue"}),
                Hit("d2", 0.70, {"product_id": "P2", "color": "red"}),
                Hit("d3", 0.40, {"product_id": "P3", "color": "red"})]  # below MIN_SCORE
        return [h for h in hits if all(h.fields.get(f) == x for f, x in filters.items())]


class FakeCatalog(CatalogStore):
    P = {"P1": {"product_id": "P1", "name": "Oxford", "category": "shirts", "active": True},
         "P2": {"product_id": "P2", "name": "Tee", "category": "tops", "active": True},
         "P3": {"product_id": "P3", "name": "Old", "active": False}}
    VAR = {"P1": [{"sku": "P1-B-M", "barcode": "111", "color": "blue", "size": "M", "product_id": "P1"},
                  {"sku": "P1-B-XL", "barcode": "222", "color": "blue", "size": "XL", "product_id": "P1"}],
           "P2": [{"sku": "P2-R", "color": "red", "size": "S", "product_id": "P2"}]}

    def get_products(self, ids): return {i: self.P[i] for i in ids if i in self.P}
    def get_variants(self, pid): return list(self.VAR.get(pid, []))

    def find_variant_by_barcode(self, code):
        return next((v for vs in self.VAR.values() for v in vs if v.get("barcode") == code), None)

    def upsert_product(self, p): pass
    def upsert_variant(self, v): pass
    def get_registry(self, pid, color): return None
    def put_registry(self, pid, color, rec): pass


class FakeInventory(InventoryProvider):  # stands in for an external OMS/WMS
    def get_stock(self, variants, store_ids):
        return {"P1-B-M": {"st1": 0, "st2": 5}, "P1-B-XL": {"st1": 2, "st2": 0}}


class FakeOcr(TextReader):
    def __init__(self, lines): self.lines = lines
    def read_lines(self, image): return self.lines


def svc(ocr=None):
    return Services(FakeEmbedder(), FakeVectors(), FakeCatalog(), FakeInventory(), None, ocr)


IMG = "/9j/4AAQ"  # any valid base64

# ------------------------------------------------------------------ engine end to end
out = run_match(svc(FakeOcr([("XL", 97)])), {"image_base64": IMG, "store_id": "st1"})
m = out["matches"]
assert [x["product"]["product_id"] for x in m] == ["P1", "P2"]  # P3 filtered: below score and inactive
assert out["detected"] == {"size": "XL", "size_source": "ocr", "barcode_found": None}
assert m[0]["variant_resolution"]["matched_variant"]["sku"] == "P1-B-XL" and m[0]["confidence"] == "high"
assert m[0]["variant_resolution"]["matched_variant"]["stock_qty"] == 2   # stock came from the inventory port

out = run_match(svc(), {"image_base64": IMG, "store_id": "st1"})  # no size: only XL in stock at st1
assert out["matches"][0]["variant_resolution"]["status"] == "inferred_by_stock"

out = run_match(svc(), {"image_base64": IMG, "category": "tops"})  # filters reach the vector store
assert [x["product"]["product_id"] for x in out["matches"]] == []  # FakeVectors filters on fields it has

out = run_match(svc(), {"barcode": "222"})  # barcode only
assert out["matches"][0]["matched_by"] == "barcode"
assert out["matches"][0]["variant_resolution"]["matched_variant"]["sku"] == "P1-B-XL"
out = run_match(svc(), {"barcode": "222", "image_base64": IMG})
assert out["matches"][0]["image_agrees"] is True

for bad, exc in [({}, ValueError), ({"image_base64": "!!!"}, ValueError), ({"barcode": "nope"}, LookupError),
                 ({"image_s3_key": "queries/x.jpg"}, ValueError)]:
    try:
        run_match(svc(), bad)
        raise AssertionError(f"expected {exc.__name__} for {bad}")
    except exc:
        pass

# ------------------------------------------------------------------ handlers
sys.modules.setdefault("boto3", types.ModuleType("boto3"))  # handlers import config only; no AWS used
config.services.cache_clear()
config.services = lambda: svc()  # handlers did `from vsearch.config import services`, so patch before import
import inventory as inv_h, search as search_h  # noqa: E402
search_h.services = inv_h.services = lambda: svc()
ctx = types.SimpleNamespace(aws_request_id="req-1")

res = search_h.handler({"body": json.dumps({"image_base64": IMG})}, ctx)
assert res["statusCode"] == 200 and json.loads(res["body"])["matches"]
assert search_h.handler({"body": "{}"}, ctx)["statusCode"] == 400
assert search_h.handler({"body": json.dumps({"barcode": "nope"})}, ctx)["statusCode"] == 404

res = inv_h.handler({"body": json.dumps({"image_base64": IMG, "size": "M", "store_id": "st1"})}, ctx)
body = json.loads(res["body"])
a = body["items"][0]["availability"]
assert res["statusCode"] == 200 and a["matched_sku"] == "P1-B-M" and a["in_stock"] is False
assert a["other_stores"] == [{"store_id": "st2", "qty": 5}]
assert inv_h.handler({"body": json.dumps({"image_base64": IMG})}, ctx)["statusCode"] == 400  # store_id required

# ------------------------------------------------------------------ adapter selection
for k in ("VECTOR_STORE", "CATALOG_STORE"):
    os.environ.pop(k, None)
assert config.REGISTRY["vector_store"]["options"].keys() >= {"opensearch", "solr"}
assert config.REGISTRY["catalog"]["options"].keys() >= {"dynamodb", "mysql"}
os.environ["VECTOR_STORE"] = "test_logic:FakeVectors"  # dotted path to a custom adapter
sys.modules["test_logic"] = sys.modules["__main__"]
assert isinstance(config._build("vector_store"), FakeVectors)
config.register("catalog", "fake", FakeCatalog)
os.environ["CATALOG_STORE"] = "fake"
assert isinstance(config._build("catalog"), FakeCatalog)
os.environ["CATALOG_STORE"] = "oracle"
try:
    config._build("catalog")
    raise AssertionError("unknown backend should raise")
except ValueError as e:
    assert "unknown catalog 'oracle'" in str(e)

print("all logic checks passed")
