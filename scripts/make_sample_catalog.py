#!/usr/bin/env python3
"""Builds a test catalog feed (CSV + images + test queries) for the visual search API. Standard library only.

  synthetic   no downloads. Draws simple patterned shirts in several colours. Good for a first smoke test of the
              whole pipeline (colour and size variants, barcodes, stock). NOT a test of real-world accuracy.
  kaggle      converts the public "Fashion Product Images (Small)" dataset (styles.csv + images/) into a feed.
              Download it yourself and check its license and image size (see README).

Output folder:
  catalog.csv    one row per variant (SKU), ready for scripts/enqueue_catalog.py
  images/        catalog photos, to upload to s3://<bucket>/catalog/
  queries/       test photos to send to the API (re-rendered / lightly altered, not identical to the catalog photos)
  expected.csv   ground truth for scripts/evaluate.py

Examples:
  python scripts/make_sample_catalog.py synthetic --out sample --products 12
  python scripts/make_sample_catalog.py kaggle --styles styles.csv --images-dir images --out sample --limit 300
"""
import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import struct
import sys
import zlib

SIZES = ["S", "M", "L", "XL"]
STORES = ["store1", "store2", "store3"]
COLUMNS = ["product_id", "name", "brand", "category", "description", "url", "color", "size", "sku",
           "barcode", "price", "currency", "stock", "image_s3_key"]
EXPECTED_COLUMNS = ["query_file", "product_id", "color", "size", "sku"]


def slug(s):
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def barcode(sku):
    return f"{int(hashlib.sha1(sku.encode()).hexdigest(), 16) % 10 ** 12:012d}"


def variant_rows(product, color, image_key):
    """One row per size for a product+colour. Stock per store is deterministic per SKU."""
    rows = []
    for size in SIZES:
        sku = f"{product['product_id']}-{slug(color)}-{size}"
        rng = random.Random(sku)
        stock = {s: rng.choice([0, 0, 1, 2, 3, 5, 8]) for s in STORES}
        rows.append({**{k: product[k] for k in ("product_id", "name", "brand", "category", "description", "url")},
                     "color": color, "size": size, "sku": sku, "barcode": barcode(sku),
                     "price": product["price"], "currency": "USD", "stock": json.dumps(stock),
                     "image_s3_key": image_key})
    return rows


def write_outputs(out, rows, expected):
    with open(os.path.join(out, "catalog.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out, "expected.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        w.writeheader()
        w.writerows(expected)
    print(f"wrote {len(rows)} variant rows, {len(expected)} test queries to {out}/")
    print("next:")
    print(f"  aws s3 sync {out}/images s3://<ImageBucketName>/catalog/")
    print(f"  QUEUE_URL=<IngestQueueUrl> python scripts/enqueue_catalog.py {out}/catalog.csv")
    print(f"  API_URL=<SearchUrl> API_KEY=<key> python scripts/evaluate.py {out}")


# ----------------------------------------------------------------------------- synthetic
PALETTE = {"blue": (40, 90, 200), "red": (200, 45, 45), "green": (40, 150, 70), "black": (30, 30, 30),
           "yellow": (235, 200, 40), "purple": (130, 60, 170), "orange": (235, 130, 30), "teal": (30, 150, 150)}
ACCENTS = [(250, 250, 250), (25, 25, 25)]
PATTERNS = ["solid", "hstripes", "vstripes", "checks", "dots"]


def png_bytes(w, h, pixels):
    raw = b"".join(b"\x00" + bytes(pixels[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render_shirt(pattern, base, accent, step, jitter=None, size=256):
    """A simple T-shirt silhouette filled with a pattern. `jitter` = random.Random for a slightly altered photo."""
    dx = dy = 0
    gain, noise = 1.0, 0
    if jitter:
        dx, dy = jitter.randint(-8, 8), jitter.randint(-8, 8)
        gain, noise = jitter.uniform(0.88, 1.12), 6
    nrng = random.Random(1)
    pix = bytearray(size * size * 3)
    for y in range(size):
        for x in range(size):
            u, v = x - dx, y - dy
            in_body = 88 <= u < 168 and 70 <= v < 220
            in_sleeve = 48 <= u < 208 and 70 <= v < 110
            neck = 118 <= u < 138 and 70 <= v < 84
            if (in_body or in_sleeve) and not neck:
                if pattern == "hstripes":
                    on = (v // step) % 2
                elif pattern == "vstripes":
                    on = (u // step) % 2
                elif pattern == "checks":
                    on = ((u // step) + (v // step)) % 2
                elif pattern == "dots":
                    on = ((u % (2 * step)) - step) ** 2 + ((v % (2 * step)) - step) ** 2 < (step * 0.45) ** 2
                else:
                    on = 0
                c = accent if on else base
            else:
                c = (236, 236, 236)
            n = nrng.randint(-noise, noise) if noise else 0
            i = (y * size + x) * 3
            pix[i:i + 3] = bytes(max(0, min(255, int(ch * gain) + n)) for ch in c)
    return png_bytes(size, size, pix)


def make_synthetic(args):
    rng = random.Random(args.seed)
    os.makedirs(os.path.join(args.out, "images"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "queries"), exist_ok=True)
    rows, candidates = [], []
    for i in range(args.products):
        pid = f"SYN{i + 1:03d}"
        pattern, step = PATTERNS[i % len(PATTERNS)], 8 + (i % 3) * 4
        accent = ACCENTS[(i // len(PATTERNS)) % len(ACCENTS)]
        product = {"product_id": pid, "name": f"{pattern.title()} Shirt {i + 1}", "brand": "TestBrand",
                   "category": "shirts", "description": f"Synthetic {pattern} shirt", "url": f"https://example.com/{pid}",
                   "price": 20 + (i * 7) % 60}
        for color in rng.sample(sorted(PALETTE), args.colors):
            d = os.path.join(args.out, "images", pid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{slug(color)}.png"), "wb") as f:
                f.write(render_shirt(pattern, PALETTE[color], accent, step))
            rows += variant_rows(product, color, f"catalog/{pid}/{slug(color)}.png")
            candidates.append((pid, color, pattern, step, accent))
    expected = []
    for pid, color, pattern, step, accent in rng.sample(candidates, min(args.queries, len(candidates))):
        size = rng.choice(SIZES)
        qf = f"{pid}-{slug(color)}.png"
        with open(os.path.join(args.out, "queries", qf), "wb") as f:
            f.write(render_shirt(pattern, PALETTE[color], accent, step, jitter=rng))
        expected.append({"query_file": f"queries/{qf}", "product_id": pid, "color": color.lower(),
                         "size": size, "sku": f"{pid}-{slug(color)}-{size}"})
    write_outputs(args.out, rows, expected)


# ----------------------------------------------------------------------------- kaggle
def read_styles(path):
    """styles.csv has some rows with unquoted commas in the name; fold the extras back into the last column."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        r = csv.reader(f)
        header = next(r)
        n = len(header)
        for row in r:
            if len(row) > n:
                row = row[:n - 1] + [",".join(row[n - 1:])]
            if len(row) == n:
                yield dict(zip(header, row))


def augment(src, dst, rng):
    """Light crop/rotate/brightness change with Pillow if installed; otherwise a plain copy."""
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        shutil.copyfile(src, dst)
        return False
    try:
        im = Image.open(src).convert("RGB")
        w, h = im.size
        k = rng.uniform(0.86, 0.96)
        cx, cy = rng.randint(0, int(w * (1 - k))), rng.randint(0, int(h * (1 - k)))
        im = im.crop((cx, cy, cx + int(w * k), cy + int(h * k))).rotate(rng.uniform(-8, 8), expand=True,
                                                                      fillcolor=(255, 255, 255))
        ImageEnhance.Brightness(im).enhance(rng.uniform(0.85, 1.15)).save(dst, quality=90)
        return True
    except Exception as e:  # unreadable image: keep the original so the run still completes
        print(f"warning: could not alter {src} ({e}); copied as is")
        shutil.copyfile(src, dst)
        return False


def make_kaggle(args):
    rng = random.Random(args.seed)
    types = {t.strip().lower() for t in args.article_types.split(",")} if args.article_types else None
    eligible = []
    for r in read_styles(args.styles):
        if r.get("masterCategory") != "Apparel" or not r.get("baseColour"):
            continue
        if types and r.get("articleType", "").lower() not in types:
            continue
        img = os.path.join(args.images_dir, f"{r['id']}.jpg")
        if os.path.exists(img):
            eligible.append((r, img))
    if not eligible:
        sys.exit("no usable rows: check --styles/--images-dir (images must be named <id>.jpg)")
    rng.shuffle(eligible)
    chosen = eligible[:args.limit]
    os.makedirs(os.path.join(args.out, "images"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "queries"), exist_ok=True)
    rows, picked = [], []
    for r, img in chosen:
        pid, color = f"K{r['id']}", r["baseColour"]
        product = {"product_id": pid, "name": r.get("productDisplayName") or r["articleType"], "brand": "Sample",
                   "category": r["articleType"].lower(), "description": f"{r['gender']} {r['articleType']}",
                   "url": f"https://example.com/{pid}", "price": 15 + int(r["id"]) % 100}
        d = os.path.join(args.out, "images", pid)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(img, os.path.join(d, f"{slug(color)}.jpg"))
        rows += variant_rows(product, color, f"catalog/{pid}/{slug(color)}.jpg")
        picked.append((pid, color, img))
    expected, altered = [], True
    for pid, color, img in rng.sample(picked, min(args.queries, len(picked))):
        size = rng.choice(SIZES)
        qf = f"queries/{pid}.jpg"
        altered &= augment(img, os.path.join(args.out, qf), rng)
        expected.append({"query_file": qf, "product_id": pid, "color": color.lower(), "size": size,
                         "sku": f"{pid}-{slug(color)}-{size}"})
    if not altered:
        print("note: Pillow not installed, so test queries are exact copies of catalog images (a plumbing test only).\n"
              "      `pip install pillow` and re-run for altered queries, or add real phone photos to queries/.")
    write_outputs(args.out, rows, expected)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("synthetic")
    s.add_argument("--out", default="sample")
    s.add_argument("--products", type=int, default=12)
    s.add_argument("--colors", type=int, default=3, help="colours per product (1-8)")
    s.add_argument("--queries", type=int, default=20)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=make_synthetic)
    k = sub.add_parser("kaggle")
    k.add_argument("--styles", required=True, help="path to styles.csv")
    k.add_argument("--images-dir", required=True, help="folder with <id>.jpg files")
    k.add_argument("--out", default="sample")
    k.add_argument("--limit", type=int, default=300, help="products to include")
    k.add_argument("--queries", type=int, default=30)
    k.add_argument("--article-types", default="", help="comma list, e.g. Shirts,Tshirts,Dresses (default: all apparel)")
    k.add_argument("--seed", type=int, default=7)
    k.set_defaults(fn=make_kaggle)
    args = p.parse_args()
    if args.mode == "synthetic" and not 1 <= args.colors <= len(PALETTE):
        sys.exit(f"--colors must be 1-{len(PALETTE)}")
    args.fn(args)


if __name__ == "__main__":
    main()
