# Visual Search API

Image (and/or barcode) in → matching catalog product + exact variant out. One engine, several use cases
(product search, store inventory check), pluggable backends. Default deployment is all-AWS.

## Architecture: ports and adapters

```
 handlers (search, inventory/check, ingest)     <- thin: parse request, shape response; add use cases here
            │
   vsearch/pipeline.py  run_match()             <- shared engine: embed -> k-NN -> products/variants -> resolve variant
            │
   vsearch/ports.py   Embedder · VectorStore · CatalogStore · InventoryProvider · TextReader · ImageStore
            │
   vsearch/adapters/  bedrock | opensearch · solr | dynamodb · mysql | embedded_stock | rekognition | s3
```

The core (`pipeline.py`, `matching.py`) imports no AWS code. Backends are selected by environment variables
(CDK sets them from context flags):

| Port | Env var | Built-in options (default first) |
|---|---|---|
| Embedder | `EMBEDDER` | `bedrock` |
| VectorStore | `VECTOR_STORE` | `opensearch` (Serverless or managed domain), `solr` |
| CatalogStore | `CATALOG_STORE` | `dynamodb`, `mysql` (Aurora/RDS MySQL) |
| InventoryProvider | `INVENTORY` | `embedded` (stock on variant rows) |
| TextReader (size OCR) | `OCR` | `rekognition`, `none` |
| ImageStore | `IMAGE_STORE` | `s3` |

**Add your own backend:** implement the port in `ports.py`, then either set the env var to a dotted path
(`VECTOR_STORE=mypkg.mod:MyStore`; the class needs a no-arg constructor or `from_env()`) or call
`vsearch.config.register("vector_store", "mine", MyStore)`. No core changes. Contract notes:
- `VectorStore.knn` returns `Hit(id, score, fields)` with score as **cosine similarity in [0,1]**. Each adapter converts its engine's native score.
- Vectors are unit-length, so L2 and cosine rank identically. Dimension comes from the embedder.
- Vector stores that lack upsert/custom ids are fine: the `CatalogStore` registry tracks document ids.

## Variants (the XL shirt case)

A photo shows style and colour, not size. The index holds one vector per (product, colour); size comes from:

1. `barcode` (one exact SKU; with a photo it also cross-checks: `image_agrees`)
2. `size` in the request (tag, picker, shopper)
3. OCR of a size tag in the photo (one unambiguous token, ≥85% confidence)
4. stock: with `store_id`, a single in-stock size → `inferred_by_stock` (a suggestion)

`variant_resolution.status`: `exact` · `inferred_by_stock` · `size_required` (returns `available_sizes`, `in_stock_sizes`) ·
`size_not_available` · `no_variants`.

## Use cases (endpoints)

Both take the same inputs: `image_base64` | `image_s3_key`, `barcode`, `size`, `store_id`, `category`, `brand`, `top_k`.
Header `x-api-key`.

- **`POST /v1/search`**: product + variant details (see above).
- **`POST /v1/inventory/check`**: "do we have it?" `store_id` required. Per match: stock of the resolved SKU at that store,
  quantity per size, and other stores holding the SKU (`max_other_stores`, default 5).

```json
{ "image_base64": "…", "size": "M", "store_id": "st1" }
→ { "items": [{ "product": {…}, "status": "exact", "size": "M",
     "availability": { "store_id": "st1", "matched_sku": "P1-B-M", "qty": 0, "in_stock": false,
       "sizes": [{"size":"M","qty":0,"in_stock":false},{"size":"XL","qty":2,"in_stock":true}],
       "other_stores": [{"store_id":"st2","qty":5}] } }] }
```

**New use case** (e.g. "find similar", price check, returns lookup): copy `lambdas/inventory.py`, call
`run_match(services(), req)`, shape the output, add a Lambda + route in the stack.

**Stock source:** default reads `stock` from variant rows (number or `{store_id: qty}`). For a live OMS/WMS, implement
`InventoryProvider.get_stock(variants, store_ids)` → `{sku: qty | {store_id: qty}}` and set `INVENTORY=pkg.mod:Class`.

## Deploy

```bash
pip install -r requirements.txt
cdk bootstrap
cdk deploy                                   # OpenSearch Serverless + DynamoDB (default)
```
Enable the embedding model in the Bedrock console first. Flags:

| Flag | Effect |
|---|---|
| `-c vector_store=solr -c solr_url=http://host:8983 [-c solr_secret_arn=…]` | Solr 9.x instead of OpenSearch |
| `-c catalog_store=mysql -c mysql_secret_arn=…` | MySQL/Aurora instead of DynamoDB (run `scripts/build_mysql_layer.sh` first; apply `sql/schema.mysql.sql`) |
| `-c vpc_id=… -c subnet_ids=a,b -c security_group_ids=sg` | Put Lambdas in a VPC (needed to reach private Solr/MySQL) |
| `-c search_provisioned=5` | Provisioned concurrency for POS latency |

Lambdas in a VPC need a NAT gateway or VPC endpoints for Bedrock, Rekognition, S3 and Secrets Manager.
Use RDS Proxy in front of MySQL; Lambdas open many short connections.

Load the catalog:
```bash
aws s3 sync ./images s3://<ImageBucketName>/catalog/
QUEUE_URL=<IngestQueueUrl> python scripts/enqueue_catalog.py catalog.csv
```
CSV: `product_id,name,brand,category,description,url,color,size,sku,barcode,price,currency,stock,image_s3_key`
(`stock` = integer, or JSON per store such as `{"store1": 3, "store2": 0}`).

## Tear down (after testing)

```bash
scripts/teardown.sh --dry-run     # lists what would be deleted; changes nothing
scripts/teardown.sh               # asks you to type the stack name, then deletes
```
Needs only the AWS CLI. It deletes the CloudFormation stack (API, Lambdas, queues, roles, OpenSearch collection), then the
resources CDK keeps on destroy (S3 image bucket, DynamoDB tables) and the Lambda log groups, and verifies nothing is left.
Options: `--stack NAME`, `--region R`, `--yes` (no prompt). Data is deleted permanently. It does not remove the shared CDK
bootstrap stack (`CDKToolkit`), nor a MySQL database or Solr server you run yourself.

## Efficiency at scale
- Index size = styles × colours, not SKUs. Unchanged images are skipped on re-sync (image-hash registry).
- Embedding and OCR run in parallel; catalog reads are batched. Everything is stateless and scales horizontally.
- Ingest concurrency is capped (`max_concurrency=10`) to stay under the embedding model's throughput quota.
- `ENABLE_OCR=false` saves cost/latency when size is always supplied.

## Caveats
- **Not run end to end.** Verified offline only (`python3 tests/test_logic.py`: engine, handlers, variant logic and adapter
  selection with in-memory fakes; MySQL row mapping and score conversions). The Bedrock, OpenSearch, Solr, DynamoDB and MySQL
  adapters and the CDK stack have not been run against live services; `cdk synth` was not run here.
- **Solr:** AWS has no managed Solr (run on EC2/ECS/EKS). Verify on your version that `fq` pre-filters apply to `{!knn}`
  (Solr 9.1+) and that the score mapping (Lucene cosine `(1+cos)/2`) matches.
- Calibrate `MIN_SCORE` / `HIGH_SCORE` (0.55 / 0.80) on real query photos; add several angles per colour.
- Switching embedding model: change `EMBED_MODEL_ID`/`EMBED_DIM` (and the request body in `adapters/bedrock.py` if its schema differs), then re-index.
  Verify the model is active in your region: `aws bedrock get-foundation-model --model-identifier amazon.titan-embed-image-v1 --query modelDetails.modelLifecycle`.
- Switching vector store or database means re-running ingestion into the new backend; there is no live migration.
- OpenSearch endpoint is public but IAM-authenticated; for production use a VPC endpoint and add AWS WAF.
- Numeric sizes (shoes, waist) work via barcode or the `size` hint; OCR reads letter sizes only.
