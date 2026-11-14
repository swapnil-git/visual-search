"""OpenSearch vector store: Serverless collection (service 'aoss', default) or managed domain ('es').

Vector collections do not support custom document ids, so ids are generated and tracked by the
catalog registry."""
import hashlib
import json
import os
import urllib.error
import urllib.request

from ..ports import Hit, VectorStore


def _l2_to_cosine(score):
    """Faiss L2 score = 1/(1+d^2); for unit vectors cosine = 1 - d^2/2."""
    if score <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (1.0 / score - 1.0) / 2.0))


class OpenSearchVectorStore(VectorStore):
    def __init__(self, endpoint, index="products", region=None, service="aoss"):
        self.endpoint, self.index = endpoint.rstrip("/"), index
        self.region, self.service = region or os.environ.get("AWS_REGION", "us-east-1"), service
        self._session = None

    @classmethod
    def from_env(cls):
        return cls(os.environ["AOSS_ENDPOINT"], os.environ.get("VECTOR_INDEX", "products"),
                   os.environ.get("AWS_REGION"), os.environ.get("OPENSEARCH_SERVICE", "aoss"))

    def _call(self, method, path, body=None):
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        self._session = self._session or boto3.Session()
        url = f"{self.endpoint}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json",
                   "x-amz-content-sha256": hashlib.sha256(data or b"").hexdigest()}  # required by AOSS
        req = AWSRequest(method=method, url=url, data=data, headers=headers)
        SigV4Auth(self._session.get_credentials(), self.service, self.region).add_auth(req)
        r = urllib.request.Request(url, data=data, method=method, headers=dict(req.headers))
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")

    def ensure_index(self, dim):
        try:
            self._call("GET", f"/{self.index}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            self._call("PUT", f"/{self.index}", {
                "settings": {"index.knn": True},
                "mappings": {"properties": {
                    "embedding": {"type": "knn_vector", "dimension": dim,
                                  "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2",
                                             "parameters": {"ef_construction": 128, "m": 16}}},
                    "product_id": {"type": "keyword"}, "color": {"type": "keyword"},
                    "category": {"type": "keyword"}, "brand": {"type": "keyword"},
                    "image_key": {"type": "keyword"}}}})

    def add(self, vector, fields):
        return self._call("POST", f"/{self.index}/_doc", {**fields, "embedding": vector})["_id"]

    def delete(self, doc_id):
        self._call("DELETE", f"/{self.index}/_doc/{doc_id}")

    def knn(self, vector, k, filters):
        knn = {"vector": vector, "k": k}
        if filters:
            knn["filter"] = {"bool": {"filter": [{"term": {f: v}} for f, v in filters.items()]}}
        body = {"size": k, "_source": ["product_id", "color", "image_key"],
                "query": {"knn": {"embedding": knn}}}
        hits = self._call("POST", f"/{self.index}/_search", body)["hits"]["hits"]
        return [Hit(h["_id"], _l2_to_cosine(h["_score"]), h["_source"]) for h in hits]
