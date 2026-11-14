"""DynamoDB catalog. Uses the low-level client (thread-safe) with the standard (de)serializers."""
import os
from concurrent.futures import ThreadPoolExecutor

from ..ports import CatalogStore


class DynamoDbCatalog(CatalogStore):
    def __init__(self, products, variants, registry, barcode_index="barcode-index"):
        import boto3
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self.products, self.variants, self.registry, self.barcode_index = products, variants, registry, barcode_index
        self._c, self._ser, self._des = boto3.client("dynamodb"), TypeSerializer(), TypeDeserializer()

    @classmethod
    def from_env(cls):
        return cls(os.environ["PRODUCTS_TABLE"], os.environ["VARIANTS_TABLE"], os.environ["REGISTRY_TABLE"],
                   os.environ.get("VARIANTS_BARCODE_INDEX", "barcode-index"))

    def _item(self, raw):
        return {k: self._des.deserialize(v) for k, v in raw.items()}

    def _raw(self, item):
        return {k: self._ser.serialize(v) for k, v in item.items() if v is not None}

    def get_products(self, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        r = self._c.batch_get_item(RequestItems={self.products: {"Keys": [{"product_id": {"S": i}} for i in ids]}})
        items = list(r["Responses"].get(self.products, []))
        if r.get("UnprocessedKeys"):
            items += self._c.batch_get_item(RequestItems=r["UnprocessedKeys"])["Responses"].get(self.products, [])
        return {p["product_id"]: p for p in map(self._item, items)}

    def get_variants(self, product_id):
        r = self._c.query(TableName=self.variants, KeyConditionExpression="product_id = :p",
                          ExpressionAttributeValues={":p": {"S": product_id}}, Limit=200)
        return [self._item(i) for i in r["Items"]]

    def get_variants_many(self, product_ids):
        if not product_ids:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(product_ids))) as ex:
            return dict(zip(product_ids, ex.map(self.get_variants, product_ids)))

    def find_variant_by_barcode(self, code):
        r = self._c.query(TableName=self.variants, IndexName=self.barcode_index,
                          KeyConditionExpression="barcode = :b",
                          ExpressionAttributeValues={":b": {"S": code}}, Limit=1)
        return self._item(r["Items"][0]) if r["Items"] else None

    def upsert_product(self, product):
        self._c.put_item(TableName=self.products, Item=self._raw(product))

    def upsert_variant(self, variant):
        self._c.put_item(TableName=self.variants, Item=self._raw(variant))

    def get_registry(self, product_id, color):
        r = self._c.get_item(TableName=self.registry,
                             Key={"product_id": {"S": product_id}, "color": {"S": color}})
        return self._item(r["Item"]) if r.get("Item") else None

    def put_registry(self, product_id, color, record):
        self._c.put_item(TableName=self.registry,
                         Item=self._raw({**record, "product_id": product_id, "color": color}))
