"""MySQL / Aurora MySQL catalog via PyMySQL (bundle it: see README; schema in sql/schema.mysql.sql).

Extra product/variant fields that are not columns are kept in the `attributes` JSON column, so
the same message format works for both catalog backends. Use RDS Proxy in front of the database
to absorb Lambda connection churn."""
import json
import os

from ..http import DecimalEncoder
from ..ports import CatalogStore
from ._secrets import load_secret

PRODUCT_COLS = ["product_id", "name", "brand", "category", "description", "price", "currency", "url", "active"]
VARIANT_COLS = ["product_id", "sku", "barcode", "color", "size", "price", "currency", "stock"]


def _json(v):
    if isinstance(v, (str, bytes)):
        return json.loads(v)
    return v


def _row_to_product(r):
    attrs = _json(r.pop("attributes", None)) or {}
    if "active" in r:
        r["active"] = bool(r["active"])
    return {**attrs, **{k: v for k, v in r.items() if v is not None}}


def _row_to_variant(r):
    attrs = _json(r.pop("attributes", None)) or {}
    if r.get("stock") is not None:
        r["stock"] = _json(r["stock"])
    return {**attrs, **{k: v for k, v in r.items() if v is not None}}


def _upsert_sql(table, cols, key_cols):
    updates = ",".join(f"{c}=VALUES({c})" for c in cols if c not in key_cols)
    return (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
            f"ON DUPLICATE KEY UPDATE {updates}")  # VALUES() keeps MySQL 5.7 / Aurora 2 compatibility


class MySqlCatalog(CatalogStore):
    def __init__(self, host, user, password, database, port=3306):
        self._args = dict(host=host, user=user, password=password, database=database, port=port,
                          autocommit=True, connect_timeout=5, charset="utf8mb4")
        self._conn = None

    @classmethod
    def from_env(cls):
        s = load_secret(os.environ["MYSQL_SECRET_ARN"]) if os.environ.get("MYSQL_SECRET_ARN") else {}
        return cls(os.environ.get("MYSQL_HOST") or s["host"],
                   os.environ.get("MYSQL_USER") or s["username"],
                   os.environ.get("MYSQL_PASSWORD") or s["password"],
                   os.environ.get("MYSQL_DB") or s.get("dbname", "catalog"),
                   int(os.environ.get("MYSQL_PORT") or s.get("port", 3306)))

    def _db(self):
        import pymysql
        if self._conn is None:
            self._conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._args)
        else:
            self._conn.ping(reconnect=True)
        return self._conn

    def _query(self, sql, params=()):
        with self._db().cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def get_products(self, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        rows = self._query(f"SELECT * FROM products WHERE product_id IN ({','.join(['%s'] * len(ids))})", ids)
        return {r["product_id"]: _row_to_product(r) for r in rows}

    def get_variants(self, product_id):
        return [_row_to_variant(r) for r in
                self._query("SELECT * FROM variants WHERE product_id=%s LIMIT 200", (product_id,))]

    def get_variants_many(self, product_ids):
        out = {pid: [] for pid in product_ids}
        if product_ids:
            rows = self._query(f"SELECT * FROM variants WHERE product_id IN ({','.join(['%s'] * len(product_ids))})",
                               list(product_ids))
            for r in rows:
                out[r["product_id"]].append(_row_to_variant(r))
        return out

    def find_variant_by_barcode(self, code):
        rows = self._query("SELECT * FROM variants WHERE barcode=%s LIMIT 1", (code,))
        return _row_to_variant(rows[0]) if rows else None

    def _upsert(self, table, cols, item):
        row = {c: item[c] for c in cols if c in item}
        extra = {k: v for k, v in item.items() if k not in cols}
        if "stock" in row:
            row["stock"] = json.dumps(row["stock"], cls=DecimalEncoder)
        if "active" in row:
            row["active"] = int(bool(row["active"]))
        row["attributes"] = json.dumps(extra, cls=DecimalEncoder)
        keys = list(row)
        self._query(_upsert_sql(table, keys, {"product_id", "sku"}), [row[k] for k in keys])

    def upsert_product(self, product):
        self._upsert("products", PRODUCT_COLS, product)

    def upsert_variant(self, variant):
        self._upsert("variants", VARIANT_COLS, variant)

    def get_registry(self, product_id, color):
        rows = self._query("SELECT image_hash, meta, doc_id FROM index_registry WHERE product_id=%s AND color=%s",
                           (product_id, color))
        return rows[0] if rows else None

    def put_registry(self, product_id, color, record):
        self._query("INSERT INTO index_registry (product_id, color, image_hash, meta, doc_id) "
                    "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE image_hash=VALUES(image_hash), "
                    "meta=VALUES(meta), doc_id=VALUES(doc_id)",
                    (product_id, color, record["image_hash"], record.get("meta"), record["doc_id"]))
