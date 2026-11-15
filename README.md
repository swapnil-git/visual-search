# Visual Search API

Image (and/or barcode) in → matching catalog product + exact variant out. One engine, several use cases
(product search, store inventory check), pluggable backends. Default deployment is all-AWS.

**Design doc:** [docs/DESIGN.md](docs/DESIGN.md) (goals, architecture, data model, API contract, variant resolution,
testing strategy, scale, security, risks, rollout). This README is the hands-on guide.

## Repository layout

```
lambdas/                    search.py, inventory.py (API handlers), ingest.py (SQS-fed catalog loader)
lambdas/vsearch/            shared engine: pipeline.py, matching.py, ports.py (interfaces), config.py (adapter selection)
lambdas/vsearch/adapters/   bedrock, opensearch, solr, dynamodb, mysql, rekognition, s3, local (test-only)
visual_search_stack.py      AWS CDK stack (app.py, cdk.json)
sql/schema.mysql.sql        MySQL schema, for CATALOG_STORE=mysql
scripts/                    enqueue_catalog.py, generate_sample_catalog.py, local_server.py, e2e_test.py,
                            load_sample_catalog.sh, teardown.sh, build_mysql_layer.sh
tests/test_logic.py         offline unit tests
sample-data/                a ready-made 20-product feed, catalog images and labelled test photos
docs/DESIGN.md              design doc
.github/workflows/ci.yml    CI: unit tests, cdk synth, local end-to-end test
```

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

**Prerequisites (check before the first deploy):**

| Need | Check | Notes |
|---|---|---|
| Python 3.12 or 3.13 | `python3 --version` | Not 3.9, and not 3.14 or newer: the CDK's `typeguard` dependency breaks on newer Pythons (`ForwardRef._evaluate` error). On macOS use the python.org installer (3.13.17 is the last 3.13 with binaries); a source build needs OpenSSL, or pip fails with "ssl module is unavailable" |
| Node.js 22 or 24 (LTS) | `node --version` | Required by the CDK CLI. Newer Node (e.g. 26) only prints a warning; silence it with `export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1` |
| AWS CLI v2 + credentials | `aws sts get-caller-identity` | An IAM user whose policy allows CloudFormation, IAM, Lambda, S3, API Gateway, DynamoDB, SQS, OpenSearch Serverless and Bedrock. For a throwaway test account, AdministratorAccess is simplest; remove it afterwards |
| A region with Titan Multimodal Embeddings | `aws configure get region` | us-east-1, us-west-2, ca-central-1, eu-central-1, eu-west-1/2/3, ap-south-1, ap-southeast-2, sa-east-1. **Not** us-west-1. The stack deploys to the CLI's region, so set it first: `aws configure set region us-west-2` |
| Bedrock model invocation works | see the check below | Run it before deploying: it fails fast if your account is not enabled for Bedrock |

Check that Bedrock works in your account and region (must print JSON, not an error):
```bash
aws bedrock-runtime invoke-model --model-id amazon.titan-embed-image-v1 \
  --content-type application/json --cli-binary-format raw-in-base64-out \
  --body '{"inputText":"red shirt"}' /tmp/bedrock-check.json && head -c 200 /tmp/bedrock-check.json
```

Then, from the project folder:
```bash
python3 -m venv .venv && source .venv/bin/activate     # use python3.12 / python3.13 if python3 is older or newer
pip install -r requirements.txt
npx aws-cdk@2 bootstrap                               # once per account and region (or `cdk bootstrap` if installed globally)
npx aws-cdk@2 deploy                                  # OpenSearch Serverless + DynamoDB (default); answer y to the IAM prompt
```
The first deploy takes about 4 minutes and prints `SearchUrl`, `InventoryCheckUrl`, `ImageBucketName` and `IngestQueueUrl`. Flags:

| Flag | Effect |
|---|---|
| `-c vector_store=solr -c solr_url=http://host:8983 [-c solr_secret_arn=…]` | Solr 9.x instead of OpenSearch |
| `-c catalog_store=mysql -c mysql_secret_arn=…` | MySQL/Aurora instead of DynamoDB (run `scripts/build_mysql_layer.sh` first; apply `sql/schema.mysql.sql`) |
| `-c vpc_id=… -c subnet_ids=a,b -c security_group_ids=sg` | Put Lambdas in a VPC (needed to reach private Solr/MySQL) |
| `-c search_provisioned=5` | Provisioned concurrency for POS latency |

Lambdas in a VPC need a NAT gateway or VPC endpoints for Bedrock, Rekognition, S3 and Secrets Manager.
Use RDS Proxy in front of MySQL; Lambdas open many short connections.

Load a catalog: use the sample feed (next section) to try the system, or your own feed:
```bash
aws s3 sync ./images s3://<ImageBucketName>/catalog/
QUEUE_URL=<IngestQueueUrl> python scripts/enqueue_catalog.py catalog.csv
```
CSV: `product_id,name,brand,category,description,url,color,size,sku,barcode,price,currency,stock,image_s3_key`
(`stock` = integer, or JSON per store such as `{"store1": 3, "store2": 0}`).

## Try it on AWS with the sample feed

The repo includes a ready-made feed in [`sample-data/`](sample-data/): 20 products, 240 SKUs (3 colours x 4 sizes each), 60 catalog
images, and 44 labelled test photos. The products are drawn garments (tees, long sleeves, trousers, dresses, shorts), so this tests the
system end to end but does not measure accuracy on real products.

| File | Contents |
|---|---|
| `sample-data/catalog.csv` | The feed: one row per SKU, in the CSV format above, with barcodes and per-store stock (`store1`..`store3`) |
| `sample-data/catalog_images/<product_id>/<color>.png` | One image per product and colour; `image_s3_key` in the CSV points at `catalog/<product_id>/<color>.png` |
| `sample-data/queries/*.png`, `queries.json` | Customer-style photos (new background, tilt, lighting) and the expected answer for each |

**1. Deploy** (once; see Deploy above for prerequisites, region and the Bedrock check):
```bash
npx aws-cdk@2 bootstrap && npx aws-cdk@2 deploy
```

**2. Load the feed.** This uploads the images to the stack's S3 bucket and queues every CSV row for ingestion. It needs the AWS CLI,
credentials for the same account and region as the stack, and `pip install boto3`. `git pull` first if `sample-data/` is missing:
```bash
pip install boto3 pillow
chmod +x scripts/*.sh
scripts/load_sample_catalog.sh sample-data          # optional 2nd argument: stack name, default VisualSearchStack
```

**3. Wait for ingestion** (asynchronous, a minute or two for 240 rows). The queue is empty when both counts are 0:
```bash
QUEUE=$(aws cloudformation describe-stacks --stack-name VisualSearchStack \
        --query "Stacks[0].Outputs[?OutputKey=='IngestQueueUrl'].OutputValue" --output text)
aws sqs get-queue-attributes --queue-url "$QUEUE" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```
While the Lambda is working, `ApproximateNumberOfMessagesNotVisible` shows the in-flight messages (up to 240); it should fall to 0 within a
few minutes. If messages keep failing they land in the dead-letter queue (`IngestDLQ` in the SQS console); read the ingest Lambda's logs:
```bash
aws logs tail $(aws logs describe-log-groups --log-group-name-prefix /aws/lambda/VisualSearchStack-Ingest \
  --query "logGroups[0].logGroupName" --output text) --since 15m | tail -40
```

**4. Run the end-to-end test** against the deployed API:
```bash
BASE=$(aws cloudformation describe-stacks --stack-name VisualSearchStack \
       --query "Stacks[0].Outputs[?OutputKey=='SearchUrl'].OutputValue" --output text | sed 's#/search$##')
KEY=$(aws apigateway get-api-keys --include-values --query "items[?name=='visual-search-pos'].value" --output text)
python3 scripts/e2e_test.py --base "$BASE" --key "$KEY" --data sample-data --wait 180
```
It prints failures, a pass rate per kind (photo, photo + size, barcode, stock inference, inventory), latency, and score statistics for
tuning `MIN_SCORE` / `HIGH_SCORE`. Photo retrieval must reach 85%; barcode, stock and inventory checks must all pass. The size-tag
(`ocr`) and non-clothing (`negative`) checks are reported but never fail the run.

**5. Try it by hand.** `req` builds a request body from a photo plus extra JSON fields:
```bash
req() { printf '{"image_base64":"%s",%s}' "$(base64 < "$1" | tr -d '\n')" "$2"; }
call() { curl -s -X POST "$BASE/$1" -H "x-api-key: $KEY" -H "Content-Type: application/json" -d @-; }

# photo + size: expect P014 (teal trousers), SKU P014-TEA-30, status "exact"
req sample-data/queries/s007.png '"size":"30"' | call search

# barcode only: expect SKU P001-GRY-S
echo '{"barcode":"890000000001"}' | call search

# stock inference: at store1 only size L of P001 (grey) is in stock -> status "inferred_by_stock", SKU P001-GRY-L
req sample-data/queries/stock001.png '"store_id":"store1"' | call search

# inventory check: P002 (yellow) XL is out of stock at store1 -> in_stock false, other_stores lists store2
req sample-data/queries/inv001.png '"store_id":"store1","size":"XL"' | call inventory/check
```
The scripted scenarios are listed in `sample-data/queries.json` (ids `stock001` and `inv001`).

**6. Clean up** when done (the stack has a standing cost): `scripts/teardown.sh --dry-run`, then `scripts/teardown.sh`.

To use your own products instead, replace `sample-data/catalog.csv` and the images with yours (same columns), or load your own feed
as shown under Deploy.

## Troubleshooting a first deploy

| Symptom | Cause and fix |
|---|---|
| `aws bedrock ... The provided model identifier is invalid` | The CLI region does not offer the model (e.g. us-west-1). `aws configure set region us-west-2`, open a new terminal, and redeploy there (`cdk bootstrap` again for the new region) |
| `AccessDeniedException ... not authorized to perform: bedrock:...` | The IAM user lacks permissions. Attach a policy that allows the deploy (see Prerequisites) |
| `cdk: command not found` / `npx: command not found` | Node.js is not installed, or Terminal was not restarted after installing it |
| `AttributeError: ... 'ForwardRef' has no attribute '_evaluate'` | The venv uses a Python that is too new. Recreate it with 3.12 or 3.13 |
| `pip: ssl module is unavailable` | The Python was built without OpenSSL. Use the python.org installer instead |
| `No sample-data/catalog.csv` | Old clone: `git pull` (or `python scripts/generate_sample_catalog.py`) |
| Ingest Lambda logs `ValidationException ... Operation not allowed` on InvokeModel | Bedrock is not enabled for the AWS account (every model fails, not only Titan). Add a valid payment method, check for any pending account verification, then open an AWS Support case ("Account and billing", free) asking for Bedrock invocation in your region. IAM and the code cannot fix this |
| e2e test: `catalog not searchable after 180s` | Ingestion has not finished or is failing: check the queue counts, the dead-letter queue and the ingest Lambda logs (Try it on AWS, step 3) |
| e2e test: `HTTP 403` | Missing or wrong `x-api-key`: re-read it with the `get-api-keys` command in step 4 |

## Testing

Four levels, cheapest first:

1. **Unit tests** (seconds, no setup): `python3 tests/test_logic.py`. Variant logic, handlers, adapter selection. Runs in CI.
2. **Local end-to-end** (no AWS, free). Run the real handlers and ingest code against in-memory
   adapters, and score the answers:
   ```bash
   pip install -r scripts/requirements-dev.txt                 # Pillow
   python3 scripts/local_server.py &                           # loads sample-data/, API on http://localhost:8080/v1
   python3 scripts/e2e_test.py --base http://localhost:8080/v1 --skip ocr
   ```
   This checks the plumbing (ingest, variants, size hint, barcode, stock inference, inventory check). The local embedder is a
   crude pixel stand-in, so it says nothing about accuracy on real products. Also runs in CI.
3. **Deployed end-to-end** (real Bedrock, OpenSearch, DynamoDB). After `cdk deploy`:
   ```bash
   scripts/load_sample_catalog.sh                  # uploads images, queues the feed (needs boto3: pip install boto3)
   # then follow "Try it on AWS with the sample feed" above for the exact commands
   ```
   `--wait` polls until the asynchronous ingestion has finished. `ocr` queries need Rekognition and are soft (reported, not failed);
   the `negative` queries (non-clothing photos) are soft too. The run prints score statistics to help set `MIN_SCORE`/`HIGH_SCORE`.
4. **Your real catalog and real photos** (the only test that measures accuracy). Convert your product feed to the CSV format
   above, put 3 to 5 phone photos of each of ~30 products in `queries.json` (same format, see `generate_sample_catalog.py`),
   and run `e2e_test.py`. Report top-1 and top-3 accuracy, and tune thresholds from the score statistics.

The sample feed (`sample-data/`, regenerate with `python3 scripts/generate_sample_catalog.py --out sample-data --clean`) is synthetic (drawn tees, long sleeves, pants, dresses, shorts in colours and patterns). It has scripted
scenarios: a product where only one size is in stock at `store1` (`inferred_by_stock`), and one whose XL is out of stock at
`store1` but in stock at `store2` (inventory check). Products that share a shape and pattern never share a colour, so every query
has one right answer. Public datasets with real product photos (for example Kaggle's "Fashion Product Images (Small)" or the
Amazon Berkeley Objects dataset; check each licence) can supply real images; they lack sizes and stock, which you would synthesize.

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
