#!/usr/bin/env python3
"""Generate a synthetic product catalog feed, catalog images, and labelled test queries.

  pip install -r scripts/requirements-dev.txt
  python scripts/generate_sample_catalog.py --out sample-data --clean   # regenerates the committed feed (20 products, seed 7)

Outputs (under --out):
  catalog.csv             one row per variant (SKU): the feed that scripts/enqueue_catalog.py loads
  catalog_images/<product_id>/<color>.png   upload to s3://<bucket>/catalog/  (scripts/load_sample_catalog.sh does it)
  queries/*.png           "customer photos": same garments re-shot with a different background, scale, tilt and light
  queries.json            what each query should return; scripts/e2e_test.py checks the API against it

The garments are drawn shapes (tees, long sleeves, pants, dresses, shorts) in several colours and patterns.
That is enough to exercise every code path and to measure retrieval against a known answer key, but it is NOT
a measure of accuracy on real products: run scripts/e2e_test.py on real photos before trusting thresholds.
Deterministic for a given --seed.
"""
import argparse
import csv
import json
import os
import random
import shutil

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

S = 512
COLORS = {"red": (200, 40, 40), "blue": (40, 80, 200), "green": (40, 150, 70), "black": (30, 30, 30),
          "yellow": (235, 200, 40), "purple": (130, 60, 160), "orange": (235, 125, 30),
          "teal": (30, 160, 160), "pink": (235, 120, 170), "grey": (140, 140, 140)}
COLOR_CODE = {"red": "RED", "blue": "BLU", "green": "GRN", "black": "BLK", "yellow": "YEL", "purple": "PUR",
              "orange": "ORG", "teal": "TEA", "pink": "PNK", "grey": "GRY"}  # unique, unlike the first 3 letters
TRIMS = {"navy": (20, 30, 80), "gold": (212, 175, 55), "brown": (110, 70, 40)}
SHAPES = ["tshirt", "longsleeve", "pants", "dress", "shorts"]
PATTERNS = ["solid", "stripes", "dots", "checks"]
NAMES = {"tshirt": "T-Shirt", "longsleeve": "Long Sleeve Top", "pants": "Trousers", "dress": "Dress", "shorts": "Shorts"}
CATEGORY = {"tshirt": "tops", "longsleeve": "tops", "pants": "bottoms", "dress": "dresses", "shorts": "bottoms"}
PRICE = {"tshirt": 19.9, "longsleeve": 29.9, "pants": 49.9, "dress": 59.9, "shorts": 34.9}
SIZES = {"tshirt": ["S", "M", "L", "XL"], "longsleeve": ["S", "M", "L", "XL"], "dress": ["S", "M", "L", "XL"],
         "pants": ["30", "32", "34", "36"], "shorts": ["30", "32", "34", "36"]}
BRANDS = ["Northfield", "Aster & Oak", "Kestrel"]
STORES = ["store1", "store2", "store3"]
CSV_COLUMNS = ["product_id", "name", "brand", "category", "description", "url", "color", "size", "sku",
               "barcode", "price", "currency", "stock", "image_s3_key"]


# ----------------------------------------------------------------------------- drawing
def _mirror(poly):
    return [(S - x, y) for x, y in poly]


def parts(shape):
    """(body polygons, trim polygons) for a garment silhouette on a 512px canvas."""
    torso = [(170, 120), (342, 120), (342, 430), (170, 430)]
    waist = [(170, 92), (342, 92), (342, 128), (170, 128)]
    if shape == "tshirt":
        sl = [(170, 120), (95, 175), (125, 235), (170, 205)]
        return [torso, sl, _mirror(sl)], [[(216, 120), (296, 120), (276, 152), (236, 152)]]
    if shape == "longsleeve":
        sl = [(170, 120), (68, 290), (104, 308), (170, 196)]
        return [torso, sl, _mirror(sl)], [[(216, 120), (296, 120), (276, 152), (236, 152)]]
    if shape == "pants":
        leg = [(170, 125), (256, 125), (246, 452), (184, 452)]
        return [leg, _mirror(leg)], [waist]
    if shape == "shorts":
        leg = [(170, 125), (256, 125), (250, 300), (176, 300)]
        return [leg, _mirror(leg)], [waist]
    if shape == "dress":
        return [[(206, 100), (306, 100), (404, 440), (108, 440)]], [[(214, 185), (298, 185), (306, 215), (206, 215)]]
    raise ValueError(shape)


def _shade(c):
    lum = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
    return tuple(min(255, v + 70) for v in c) if lum < 70 else tuple(int(v * 0.68) for v in c)


def garment_layer(shape, pattern, color, trim):
    body, trims = parts(shape)
    mask = Image.new("L", (S, S), 0)
    tmask = Image.new("L", (S, S), 0)
    d, td = ImageDraw.Draw(mask), ImageDraw.Draw(tmask)
    for p in body:
        d.polygon(p, fill=255)
    for p in trims:
        d.polygon(p, fill=255)
        td.polygon(p, fill=255)
    img = Image.new("RGB", (S, S), color)
    pd, shade = ImageDraw.Draw(img), _shade(color)
    if pattern == "stripes":
        for x in range(0, S, 36):
            pd.rectangle([x, 0, x + 16, S], fill=shade)
    elif pattern == "dots":
        for row, y in enumerate(range(40, S, 56)):
            for x in range(40 + (28 if row % 2 else 0), S, 56):
                pd.ellipse([x - 9, y - 9, x + 9, y + 9], fill=shade)
    elif pattern == "checks":
        for r, y in enumerate(range(0, S, 48)):
            for c, x in enumerate(range(0, S, 48)):
                if (r + c) % 2 == 0:
                    pd.rectangle([x, y, x + 47, y + 47], fill=shade)
    img.paste(trim, (0, 0), tmask)
    layer = img.convert("RGBA")
    layer.putalpha(mask)
    return layer


def _background(bg):
    if isinstance(bg[0], tuple):  # vertical gradient (top colour, bottom colour)
        im = Image.new("RGB", (S, S))
        d = ImageDraw.Draw(im)
        for y in range(S):
            t = y / (S - 1)
            d.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(*bg)))
        return im
    return Image.new("RGB", (S, S), bg)


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # old Pillow
        return ImageFont.load_default()


def render(spec, bg=(255, 255, 255), scale=1.0, dx=0, dy=0, rot=0.0, brightness=1.0, blur=0.0, tag=None):
    layer = garment_layer(spec["shape"], spec["pattern"], COLORS[spec["color"]], TRIMS[spec["trim"]])
    if rot:
        layer = layer.rotate(rot, resample=Image.BICUBIC, center=(S / 2, S / 2))
    if scale != 1.0:
        n = int(S * scale)
        layer = layer.resize((n, n), Image.LANCZOS)
    canvas = _background(bg)
    canvas.paste(layer, ((S - layer.width) // 2 + dx, (S - layer.height) // 2 + dy), layer)
    if tag:  # a small white size label, like a hang tag, so OCR has something to read
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle([S - 190, S - 105, S - 20, S - 20], radius=10, fill=(255, 255, 255), outline=(0, 0, 0), width=3)
        box = d.textbbox((0, 0), tag, font=_font(56))
        d.text((S - 105 - (box[2] - box[0]) / 2 - box[0], S - 62 - (box[3] - box[1]) / 2 - box[1]), tag,
               font=_font(56), fill=(0, 0, 0))
    if brightness != 1.0:
        canvas = ImageEnhance.Brightness(canvas).enhance(brightness)
    if blur:
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    return canvas


def negative_image(rng):
    """Not a garment: a few shapes on a gradient, for checking that unrelated photos are not matched."""
    im = _background(((rng.randint(180, 240),) * 3, (rng.randint(90, 160),) * 3))
    d = ImageDraw.Draw(im)
    for _ in range(4):
        c = tuple(rng.randint(30, 220) for _ in range(3))
        x, y, r = rng.randint(100, 400), rng.randint(100, 400), rng.randint(30, 90)
        (d.ellipse if rng.random() < 0.5 else d.rectangle)([x - r, y - r, x + r, y + r], fill=c)
    return im


def perturb(rng):
    """A 'customer photo' look: new background, off-centre, smaller, tilted, different light."""
    bgs = [(235, 228, 215), (200, 205, 210), ((245, 245, 245), (180, 180, 185)), ((230, 220, 200), (190, 175, 150)), (250, 250, 250)]
    return dict(bg=rng.choice(bgs), scale=rng.uniform(0.72, 0.95), dx=rng.randint(-35, 35), dy=rng.randint(-35, 35),
                rot=rng.uniform(-9, 9), brightness=rng.uniform(0.88, 1.12), blur=rng.choice([0, 0, 0.6, 1.1]))


# ----------------------------------------------------------------------------- catalog
def build_products(n, rng):
    combos = [(s, p, t) for s in SHAPES for p in PATTERNS for t in TRIMS]
    if n > len(combos):
        raise SystemExit(f"--products max is {len(combos)} (unique shape x pattern x trim combinations)")
    rng.shuffle(combos)
    for want, idx in (("tshirt", 0), ("longsleeve", 1)):  # scripted scenarios need letter sizes up front
        j = next(i for i, c in enumerate(combos) if c[0] == want and i >= idx)
        combos[idx], combos[j] = combos[j], combos[idx]
    products, used = [], {}
    for i in range(n):
        shape, pattern, trim = combos[i]
        # products sharing shape and pattern never share a colour, so every query has exactly one right answer
        free = [c for c in COLORS if c not in used.setdefault((shape, pattern), set())]
        colors = rng.sample(free, 3)
        used[(shape, pattern)].update(colors)
        products.append({"product_id": f"P{i + 1:03d}", "shape": shape, "pattern": pattern, "trim": trim,
                         "colors": colors, "brand": BRANDS[i % len(BRANDS)]})
    return products


def stock_for(p, color_idx, size, rng):
    pid = p["product_id"]
    if pid == "P001" and color_idx == 0:  # at store1 only size L is in stock -> inferred_by_stock
        return {"store1": 3 if size == "L" else 0, "store2": 4, "store3": 2}
    if pid == "P002" and color_idx == 0:  # XL is out at store1 but in stock at store2
        return {"store1": 0 if size == "XL" else 2, "store2": 6 if size == "XL" else 3, "store3": 1}
    return {s: (0 if rng.random() < 0.25 else rng.randint(1, 8)) for s in STORES}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="sample-data")
    ap.add_argument("--products", type=int, default=20, help="number of products (max 60)")
    ap.add_argument("--visual-queries", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--clean", action="store_true", help="delete --out first")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    if a.clean and os.path.isdir(a.out):
        shutil.rmtree(a.out)
    for d in ("catalog_images", "queries"):
        os.makedirs(os.path.join(a.out, d), exist_ok=True)

    products = build_products(a.products, rng)
    rows, sku_of, barcode_of, counter = [], {}, {}, 0
    for p in products:
        for ci, color in enumerate(p["colors"]):
            spec = {**p, "color": color}
            rel = f"{p['product_id']}/{color}.png"
            path = os.path.join(a.out, "catalog_images", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            render(spec).save(path)
            for size in SIZES[p["shape"]]:
                counter += 1
                sku = f"{p['product_id']}-{COLOR_CODE[color]}-{size}"
                sku_of[(p["product_id"], color, size)] = sku
                barcode_of[sku] = f"8900{counter:08d}"
                rows.append({
                    "product_id": p["product_id"], "name": f"{p['brand']} {p['pattern'].title()} {NAMES[p['shape']]}",
                    "brand": p["brand"], "category": CATEGORY[p["shape"]],
                    "description": f"{p['pattern'].title()} {NAMES[p['shape']].lower()} with {p['trim']} trim",
                    "url": f"https://shop.example.com/p/{p['product_id']}", "color": color, "size": size, "sku": sku,
                    "barcode": barcode_of[sku], "price": PRICE[p["shape"]], "currency": "USD",
                    "stock": json.dumps(stock_for(p, ci, size, rng)), "image_s3_key": f"catalog/{rel}"})
    assert len({r["sku"] for r in rows}) == len(rows) and len({r["barcode"] for r in rows}) == len(rows), "duplicate SKU/barcode"
    with open(os.path.join(a.out, "catalog.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # ------------------------------------------------------------------ labelled queries
    queries = []

    def add(qid, kind, endpoint, spec=None, request=None, expect=None, soft=False, **render_kw):
        q = {"id": qid, "kind": kind, "endpoint": endpoint, "request": request or {}, "expect": expect or {}, "soft": soft}
        if spec is not None or render_kw.get("negative"):
            fn = f"queries/{qid}.png"
            img = negative_image(rng) if render_kw.get("negative") else render(spec, **render_kw)
            img.save(os.path.join(a.out, fn))
            q["file"] = fn
        queries.append(q)

    def pick():
        p = rng.choice(products)
        return p, rng.choice(p["colors"])

    for i in range(a.visual_queries):  # photo only: expect the right product and colour, size still ambiguous
        p, c = pick()
        add(f"v{i + 1:03d}", "visual", "search", {**p, "color": c}, expect={
            "product_id": p["product_id"], "color": c, "status": "size_required"}, **perturb(rng))
    for i in range(max(1, a.visual_queries // 2)):  # photo + size hint: expect that exact SKU
        p, c = pick()
        size = rng.choice(SIZES[p["shape"]])
        add(f"s{i + 1:03d}", "visual_size", "search", {**p, "color": c}, request={"size": size}, expect={
            "product_id": p["product_id"], "color": c, "size": size, "status": "exact",
            "sku": sku_of[(p["product_id"], c, size)]}, **perturb(rng))
    for i in range(3):  # barcode only
        p, c = pick()
        size = rng.choice(SIZES[p["shape"]])
        sku = sku_of[(p["product_id"], c, size)]
        add(f"b{i + 1:03d}", "barcode", "search", request={"barcode": barcode_of[sku]}, expect={
            "product_id": p["product_id"], "sku": sku})

    p1, p2 = products[0], products[1]
    add("stock001", "stock_inferred", "search", {**p1, "color": p1["colors"][0]}, request={"store_id": "store1"}, expect={
        "product_id": "P001", "status": "inferred_by_stock", "size": "L",
        "sku": sku_of[("P001", p1["colors"][0], "L")]}, **perturb(rng))
    add("inv001", "inventory", "inventory", {**p2, "color": p2["colors"][0]}, request={"store_id": "store1", "size": "XL"}, expect={
        "product_id": "P002", "sku": sku_of[("P002", p2["colors"][0], "XL")], "in_stock": False, "other_store": "store2"},
        **perturb(rng))

    for i, size in enumerate(["S", "M", "XL"]):  # size read from a tag in the photo (needs Rekognition; soft)
        cand = [p for p in products if SIZES[p["shape"]][0] == "S"]
        p = cand[i % len(cand)]
        c = p["colors"][i % 3]
        add(f"ocr{i + 1:03d}", "ocr", "search", {**p, "color": c}, tag=size, soft=True, expect={
            "product_id": p["product_id"], "color": c, "size": size}, **perturb(rng))
    for i in range(3):  # not clothing: should not be returned with high confidence (soft)
        add(f"neg{i + 1:03d}", "negative", "search", soft=True, negative=True)

    with open(os.path.join(a.out, "queries.json"), "w") as f:
        json.dump({"seed": a.seed, "queries": queries}, f, indent=1)

    kinds = {}
    for q in queries:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
    print(f"{len(products)} products, {len(rows)} variants (SKUs), {len(products) * 3} catalog images")
    print("queries:", ", ".join(f"{k}={v}" for k, v in kinds.items()))
    print(f"written to {a.out}/  (catalog.csv, catalog_images/, queries/, queries.json)")


if __name__ == "__main__":
    main()
