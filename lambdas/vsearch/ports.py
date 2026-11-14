"""Ports: the small interfaces the core depends on.

Adapters implement them; which adapter runs is chosen by environment variables (see config.py).
To support a new backend, implement the port and register it, no core changes needed.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Hit:
    id: str
    score: float  # cosine similarity in [0, 1]; 1 = identical. Adapters must convert to this.
    fields: dict = field(default_factory=dict)


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed_image(self, image: bytes) -> list:
        """Return a unit-length vector of length `dim`."""


class VectorStore(ABC):
    @abstractmethod
    def ensure_index(self, dim: int) -> None:
        """Create the index/collection/schema if it does not exist."""

    @abstractmethod
    def add(self, vector: list, fields: dict) -> str:
        """Store a vector with metadata fields (product_id, color, category, brand, image_key); return its id."""

    @abstractmethod
    def delete(self, doc_id: str) -> None: ...

    @abstractmethod
    def knn(self, vector: list, k: int, filters: dict) -> list:
        """Return up to k Hit objects, best first. `filters` = exact-match {field: value}."""


class CatalogStore(ABC):
    """Products, variants and the image-index registry."""

    @abstractmethod
    def get_products(self, ids: list) -> dict:
        """{product_id: product dict}"""

    @abstractmethod
    def get_variants(self, product_id: str) -> list: ...

    def get_variants_many(self, product_ids: list) -> dict:
        return {pid: self.get_variants(pid) for pid in product_ids}

    @abstractmethod
    def find_variant_by_barcode(self, code: str):
        """Variant dict (with product_id) or None."""

    @abstractmethod
    def upsert_product(self, product: dict) -> None: ...

    @abstractmethod
    def upsert_variant(self, variant: dict) -> None:
        """variant includes product_id and sku."""

    @abstractmethod
    def get_registry(self, product_id: str, color: str):
        """Record {image_hash, meta, doc_id} of the indexed image for this product+colour, or None."""

    @abstractmethod
    def put_registry(self, product_id: str, color: str, record: dict) -> None: ...


class InventoryProvider(ABC):
    @abstractmethod
    def get_stock(self, variants: list, store_ids) -> dict:
        """{sku: quantity | {store_id: quantity}}. Return only SKUs you know; the rest keep catalog stock."""


class TextReader(ABC):
    @abstractmethod
    def read_lines(self, image: bytes) -> list:
        """[(text, confidence 0-100)] for each detected line of text (used to read size tags)."""


class ImageStore(ABC):
    @abstractmethod
    def get(self, key: str) -> bytes: ...
