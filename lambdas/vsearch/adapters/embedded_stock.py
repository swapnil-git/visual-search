"""Default inventory source: stock stored on the variant rows (a number, or {store_id: qty}).

To use a real OMS/WMS instead, implement InventoryProvider.get_stock(variants, store_ids) by calling
its API for those SKUs and returning {sku: {store_id: qty}}; select it with INVENTORY=pkg.mod:Class."""
from ..ports import InventoryProvider


class EmbeddedStock(InventoryProvider):
    @classmethod
    def from_env(cls):
        return cls()

    def get_stock(self, variants, store_ids):
        return {v["sku"]: v["stock"] for v in variants if v.get("sku") and v.get("stock") is not None}
