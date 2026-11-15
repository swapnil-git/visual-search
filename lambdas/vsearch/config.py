"""Chooses adapters from environment variables. Defaults reproduce the original AWS-native stack.

  EMBEDDER      bedrock  | local           (default bedrock)
  VECTOR_STORE  opensearch | solr | local  (default opensearch)
  CATALOG_STORE dynamodb   | mysql | local (default dynamodb)
  INVENTORY     embedded                   (stock read from the variant rows; default)
  OCR           rekognition | none         (default rekognition)
  IMAGE_STORE   s3         | local           (default s3)

'local' = in-memory test adapters for running on a laptop without AWS (scripts/local_server.py).

Any value containing ':' is a dotted path to your own adapter, e.g.  VECTOR_STORE=mypkg.mod:MyStore
(the class needs a `from_env()` classmethod, or a no-argument constructor).
In code you can also call register("vector_store", "mine", MyStore).
"""
import importlib
import os
from functools import lru_cache

from .pipeline import Services

_A = "vsearch.adapters."
REGISTRY = {
    "embedder": {"env": "EMBEDDER", "default": "bedrock",
                 "options": {"bedrock": _A + "bedrock:BedrockTitanEmbedder",
                             "local": _A + "local:LocalEmbedder"}},
    "vector_store": {"env": "VECTOR_STORE", "default": "opensearch",
                     "options": {"opensearch": _A + "opensearch:OpenSearchVectorStore",
                                 "solr": _A + "solr:SolrVectorStore",
                                 "local": _A + "local:LocalVectorStore"}},
    "catalog": {"env": "CATALOG_STORE", "default": "dynamodb",
                "options": {"dynamodb": _A + "dynamodb:DynamoDbCatalog",
                            "mysql": _A + "mysql:MySqlCatalog",
                            "local": _A + "local:LocalCatalog"}},
    "inventory": {"env": "INVENTORY", "default": "embedded",
                  "options": {"embedded": _A + "embedded_stock:EmbeddedStock"}},
    "ocr": {"env": "OCR", "default": "rekognition",
            "options": {"rekognition": _A + "rekognition:RekognitionReader", "none": None}},
    "images": {"env": "IMAGE_STORE", "default": "s3",
               "options": {"s3": _A + "s3:S3ImageStore",
                           "local": _A + "local:LocalImageStore"}},
}


def register(kind, name, target):
    """target: a class/factory, or a 'module:Class' string."""
    REGISTRY[kind]["options"][name] = target


def _build(kind):
    spec = REGISTRY[kind]
    name = os.environ.get(spec["env"], spec["default"])
    if name in spec["options"]:
        target = spec["options"][name]
    elif ":" in name:
        target = name
    else:
        raise ValueError(f"unknown {kind} '{name}'; options: {sorted(spec['options'])} or 'module:Class'")
    if target is None:
        return None
    if isinstance(target, str):
        mod, attr = target.split(":")
        target = getattr(importlib.import_module(mod), attr)
    return getattr(target, "from_env", target)()


@lru_cache(maxsize=1)
def services() -> Services:
    return Services(embedder=_build("embedder"), vectors=_build("vector_store"),
                    catalog=_build("catalog"), inventory=_build("inventory"),
                    images=_build("images"), ocr=_build("ocr"))
