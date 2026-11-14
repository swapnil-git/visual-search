#!/usr/bin/env python3
"""Enqueue a catalog CSV for ingestion.  One CSV row = one variant (SKU).

Columns: product_id,name,brand,category,description,url,color,size,sku,barcode,price,currency,stock,image_s3_key
Usage:   QUEUE_URL=https://sqs... python scripts/enqueue_catalog.py catalog.csv
Images must already be in the catalog bucket (aws s3 sync ./images s3://<bucket>/catalog/).
"""
import csv
import json
import os
import sys
import uuid

import boto3

PRODUCT_FIELDS = ["product_id", "name", "brand", "category", "description", "url"]
VARIANT_FIELDS = ["sku", "barcode", "color", "size", "price", "currency", "stock"]


def row_to_msg(r):
    p = {k: r[k] for k in PRODUCT_FIELDS if r.get(k)}
    v = {k: r[k] for k in VARIANT_FIELDS if r.get(k)}
    if "price" in v:
        v["price"] = float(v["price"])
    if "stock" in v:  # an integer, or a JSON map per store e.g. {"store1": 3, "store2": 0}
        s = v["stock"].strip()
        v["stock"] = json.loads(s) if s.startswith("{") else int(float(s))
    return {"product": p, "variant": v, "image_s3_key": r.get("image_s3_key") or None}


def main(path):
    sqs = boto3.client("sqs")
    q = os.environ["QUEUE_URL"]
    batch, total = [], 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            batch.append({"Id": str(uuid.uuid4()), "MessageBody": json.dumps(row_to_msg(row))})
            if len(batch) == 10:
                sqs.send_message_batch(QueueUrl=q, Entries=batch)
                total += len(batch)
                batch = []
    if batch:
        sqs.send_message_batch(QueueUrl=q, Entries=batch)
        total += len(batch)
    print(f"enqueued {total} variants")


if __name__ == "__main__":
    main(sys.argv[1])
