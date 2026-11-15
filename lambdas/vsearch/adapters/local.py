"""LOCAL TEST ADAPTERS: run the whole pipeline on a laptop with no AWS (see scripts/local_server.py).

Select with EMBEDDER=local VECTOR_STORE=local CATALOG_STORE=local IMAGE_STORE=local OCR=none.
Everything is in memory and brute-force. The embedder is a crude pixel-based stand-in for a real model: it
proves the plumbing (ingest, k-NN, variants, stock, barcode, endpoints) and works on the synthetic sample
catalog, but says nothing about accuracy on real products. Needs Pillow. Not for production."""
import io
import os
import uuid

from ..ports import CatalogStore, Embedder, Hit, ImageStore, VectorStore


class LocalEmbedder(Embedder):
    dim = 2304  # 24 x 24 cells x (3 colour channels + silhouette)

    @classmethod
    def from_env(cls):
        return cls()

    def embed_image(self, image):
        """Silhouette + masked colours of the object, cropped to its bounding box (so position and scale in the
        frame do not matter). The background is estimated per row from the left/right edges (handles gradients)."""
        from PIL import Image
        im = Image.open(io.BytesIO(image)).convert("RGB")
        im.thumbnail((128, 128))
        w, h = im.size
        px = im.load()
        mask = Image.new("L", (w, h), 0)
        mp = mask.load()
        xs, ys = [], []
        for y in range(h):
            bg = [(px[0, y][i] + px[w - 1, y][i]) / 2 for i in range(3)]
            for x in range(w):
                if sum(abs(px[x, y][i] - bg[i]) for i in range(3)) > 70:
                    mp[x, y] = 255
                    xs.append(x)
                    ys.append(y)
        if xs:
            box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
            im, mask = im.crop(box), mask.crop(box)
        side = max(im.size)  # pad to a square so the aspect ratio (trousers vs shorts) is kept
        sq, sqm = Image.new("RGB", (side, side), (128, 128, 128)), Image.new("L", (side, side), 0)
        off = ((side - im.width) // 2, (side - im.height) // 2)
        sq.paste(im, off)
        sqm.paste(mask, off)
        im, mask = sq, sqm
        n = 24
        rgb, m = list(im.resize((n, n)).getdata()), list(mask.resize((n, n)).getdata())
        vec = []
        for (r, g, b), a in zip(rgb, m):
            a /= 255.0
            vec += [(r / 255.0 - 0.5) * a, (g / 255.0 - 0.5) * a, (b / 255.0 - 0.5) * a, 1.5 * (a - 0.5)]
        mean = sum(vec) / len(vec)
        vec = [v - mean for v in vec]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


class LocalVectorStore(VectorStore):
    def __init__(self):
        self._docs = {}

    @classmethod
    def from_env(cls):
        return cls()

    def ensure_index(self, dim):
        pass

    def add(self, vector, fields):
        doc_id = uuid.uuid4().hex
        self._docs[doc_id] = (vector, fields)
        return doc_id

    def delete(self, doc_id):
        self._docs.pop(doc_id, None)

    def knn(self, vector, k, filters):
        hits = []
        for doc_id, (v, fields) in self._docs.items():
            if all(fields.get(f) == x for f, x in filters.items()):
                cos = sum(a * b for a, b in zip(vector, v))
                hits.append(Hit(doc_id, max(0.0, min(1.0, cos)), fields))
        return sorted(hits, key=lambda h: -h.score)[:k]


class LocalCatalog(CatalogStore):
    def __init__(self):
        self.products, self.variants, self.registry = {}, {}, {}

    @classmethod
    def from_env(cls):
        return cls()

    def get_products(self, ids):
        return {i: self.products[i] for i in ids if i in self.products}

    def get_variants(self, product_id):
        return list(self.variants.get(product_id, {}).values())

    def find_variant_by_barcode(self, code):
        return next((v for vs in self.variants.values() for v in vs.values() if v.get("barcode") == code), None)

    def upsert_product(self, product):
        self.products[product["product_id"]] = dict(product)

    def upsert_variant(self, variant):
        self.variants.setdefault(variant["product_id"], {})[variant["sku"]] = dict(variant)

    def get_registry(self, product_id, color):
        return self.registry.get((product_id, color))

    def put_registry(self, product_id, color, record):
        self.registry[(product_id, color)] = dict(record)


class LocalImageStore(ImageStore):
    """Reads 'catalog/<pid>/<color>.png' (and 'queries/...') from a folder: LOCAL_IMAGE_ROOT/<key minus first segment>."""

    def __init__(self, root):
        self.root = root

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("LOCAL_IMAGE_ROOT", "testdata/catalog_images"))

    def get(self, key):
        rel = key.split("/", 1)[1] if "/" in key else key
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()
