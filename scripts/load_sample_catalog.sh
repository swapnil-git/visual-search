#!/usr/bin/env bash
# Loads the generated sample catalog into a DEPLOYED stack: uploads the images to S3 and queues the feed.
#   scripts/load_sample_catalog.sh [data-dir=sample-data] [stack-name=VisualSearchStack]
# Needs: AWS CLI + credentials, python3 + boto3 (pip install boto3), and `python scripts/generate_sample_catalog.py` run first.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${1:-sample-data}"; STACK="${2:-VisualSearchStack}"
[[ -f "$DATA/catalog.csv" ]] || { echo "No $DATA/catalog.csv: run scripts/generate_sample_catalog.py first." >&2; exit 1; }
out() { aws cloudformation describe-stacks --stack-name "$STACK" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET="$(out ImageBucketName)"; QUEUE="$(out IngestQueueUrl)"
[[ -n "$BUCKET" && "$BUCKET" != None && -n "$QUEUE" && "$QUEUE" != None ]] || { echo "Could not read stack outputs for $STACK." >&2; exit 1; }
echo "Uploading images to s3://$BUCKET/catalog/ ..."
aws s3 sync "$DATA/catalog_images" "s3://$BUCKET/catalog/" --only-show-errors
echo "Queuing catalog feed ..."
QUEUE_URL="$QUEUE" python3 scripts/enqueue_catalog.py "$DATA/catalog.csv"
echo "Done. Ingestion is asynchronous: run scripts/e2e_test.py with --wait 180 to wait until it is searchable."
