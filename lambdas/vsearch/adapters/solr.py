"""Apache Solr 9.x dense-vector store (DenseVectorField + the {!knn} query parser).

AWS has no managed Solr: run it on EC2/ECS/EKS and put the Lambdas in the same VPC.
Untested against a live Solr here; verify on your version, especially that fq filters act as
pre-filters for knn (Solr 9.1+ behaviour)."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ..ports import Hit, VectorStore
from ._secrets import load_secret

_FIELDS = ["product_id", "color", "category", "brand", "image_key"]


def _cosine(score):
    """Lucene scales cosine similarity to (1 + cos) / 2."""
    return max(0.0, min(1.0, 2.0 * score - 1.0))


class SolrVectorStore(VectorStore):
    def __init__(self, base_url, collection="products", user=None, password=None,
                 field="embedding", commit_within_ms=1000):
        self.base, self.collection, self.field = base_url.rstrip("/"), collection, field
        self.commit_within_ms = commit_within_ms
        self._auth = ("Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()) if user else None

    @classmethod
    def from_env(cls):
        user, pwd = os.environ.get("SOLR_USER"), os.environ.get("SOLR_PASSWORD")
        if os.environ.get("SOLR_SECRET_ARN"):
            s = load_secret(os.environ["SOLR_SECRET_ARN"])
            user, pwd = s.get("username"), s.get("password")
        return cls(os.environ["SOLR_URL"], os.environ.get("SOLR_COLLECTION", "products"), user, pwd)

    def _request(self, method, path, json_body=None, form=None):
        url = f"{self.base}/solr/{self.collection}{path}"
        data, headers = None, {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers),
                                    timeout=20) as resp:
            return json.loads(resp.read() or b"{}")

    def ensure_index(self, dim):
        try:
            self._request("GET", f"/schema/fields/{self.field}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        self._request("POST", "/schema", json_body={"add-field-type": {
            "name": "knn_vector", "class": "solr.DenseVectorField", "vectorDimension": dim,
            "similarityFunction": "cosine", "knnAlgorithm": "hnsw",
            "hnswMaxConnections": 16, "hnswBeamWidth": 128}})
        self._request("POST", "/schema", json_body={"add-field": [
            {"name": self.field, "type": "knn_vector", "indexed": True, "stored": False},
            *({"name": f, "type": "string", "indexed": True, "stored": True} for f in _FIELDS)]})

    def add(self, vector, fields):
        doc_id = str(uuid.uuid4())
        self._request("POST", f"/update/json/docs?commitWithin={self.commit_within_ms}",
                      json_body={"id": doc_id, **fields, self.field: vector})
        return doc_id

    def delete(self, doc_id):
        self._request("POST", f"/update?commitWithin={self.commit_within_ms}",
                      json_body={"delete": {"id": doc_id}})

    def knn(self, vector, k, filters):
        q = "{!knn f=%s topK=%d}[%s]" % (self.field, k, ",".join(repr(float(x)) for x in vector))
        form = {"q": q, "rows": k, "wt": "json", "fl": "id,score," + ",".join(_FIELDS),
                "fq": ["{!term f=%s}%s" % (f, v) for f, v in filters.items()]}
        docs = self._request("POST", "/select", form=form)["response"]["docs"]
        return [Hit(d["id"], _cosine(d["score"]),
                    {f: d[f] for f in _FIELDS if f in d}) for d in docs]
