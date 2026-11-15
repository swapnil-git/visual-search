#!/usr/bin/env bash
# Deletes everything the Visual Search stack created, including the resources CDK keeps on destroy
# (S3 image bucket, DynamoDB tables) and the Lambda log groups.
#
#   scripts/teardown.sh --dry-run            # list what would be deleted, change nothing
#   scripts/teardown.sh                      # asks you to type the stack name to confirm
#   scripts/teardown.sh --yes                # no prompt (for automation)
#   scripts/teardown.sh --stack MyStack --region us-west-2
#
# Uses only the AWS CLI (no CDK, Node or Python needed), so it also works if you deployed with extra
# -c flags. Resources are found by the stack's name prefix, so it also cleans up after a plain
# `cdk destroy`. DELETES DATA PERMANENTLY. Check the printed list, and the account and region, first.
#
# Not removed: the shared CDK bootstrap stack (CDKToolkit). It is cheap and other CDK apps use it.
# Not touched: a MySQL database or Solr server you run yourself, and secrets you created for them.
set -euo pipefail

STACK="VisualSearchStack"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
YES=0
DRY=0

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack)   STACK="${2:?--stack needs a value}"; shift 2 ;;
    --region)  REGION="${2:?--region needs a value}"; shift 2 ;;
    --yes)     YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

command -v aws >/dev/null || { echo "AWS CLI not found. Install it first." >&2; exit 1; }
[[ -n "$REGION" ]] || REGION="$(aws configure get region 2>/dev/null || true)"
[[ -n "$REGION" ]] || { echo "No region set: use --region or run 'aws configure'." >&2; exit 1; }
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
LOWER="$(echo "$STACK" | tr '[:upper:]' '[:lower:]')"

clean() { tr '\t' '\n' | grep -v -e '^None$' -e '^$' || true; }

discover() {
  STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK" \
            --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"
  TABLES="$(aws dynamodb list-tables --query "TableNames[?starts_with(@, '${STACK}-')]" --output text | clean)"
  BUCKETS="$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '${LOWER}-imagebucket')].Name" --output text | clean)"
  LOGS="$(aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${STACK}-" \
          --query 'logGroups[].logGroupName' --output text | clean)"
}

show() {
  echo "Account: $ACCOUNT   Region: $REGION"
  echo "CloudFormation stack ($STACK): $STATUS"
  echo "DynamoDB tables:"; [[ -n "$TABLES" ]]  && sed 's/^/  - /' <<<"$TABLES"  || echo "  (none)"
  echo "S3 buckets (emptied, then deleted):"; [[ -n "$BUCKETS" ]] && sed 's/^/  - /' <<<"$BUCKETS" || echo "  (none)"
  echo "Lambda log groups:"; [[ -n "$LOGS" ]] && sed 's/^/  - /' <<<"$LOGS" || echo "  (none)"
}

discover
show
if [[ "$STATUS" == "NONE" && -z "$TABLES$BUCKETS$LOGS" ]]; then
  echo "Nothing to delete."; exit 0
fi
if [[ "$STATUS" != "NONE" ]]; then
  echo "The stack also deletes: API Gateway + keys, Lambdas, SQS queues, IAM roles, and the OpenSearch collection."
fi
[[ "$DRY" -eq 1 ]] && { echo "Dry run: nothing deleted."; exit 0; }

if [[ "$YES" -ne 1 ]]; then
  read -r -p "Permanently delete all of the above? Type the stack name ($STACK) to confirm: " answer
  [[ "$answer" == "$STACK" ]] || { echo "Cancelled."; exit 1; }
fi

if [[ "$STATUS" != "NONE" ]]; then
  echo "Deleting stack $STACK (OpenSearch collection removal can take several minutes)..."
  aws cloudformation delete-stack --stack-name "$STACK"
  if ! aws cloudformation wait stack-delete-complete --stack-name "$STACK"; then
    echo "Stack deletion did not complete. See the Events tab of the stack in the CloudFormation console," >&2
    echo "fix the blocker, then run this script again." >&2
    exit 1
  fi
fi

for t in $TABLES; do
  echo "Deleting table $t"
  aws dynamodb delete-table --table-name "$t" >/dev/null
  aws dynamodb wait table-not-exists --table-name "$t"
done

for b in $BUCKETS; do
  echo "Emptying and deleting bucket $b"
  aws s3 rb "s3://$b" --force >/dev/null
done

for g in $LOGS; do
  echo "Deleting log group $g"
  aws logs delete-log-group --log-group-name "$g"
done

echo "--- Verifying ---"
discover
if [[ "$STATUS" == "NONE" && -z "$TABLES$BUCKETS$LOGS" ]]; then
  echo "Done: everything is deleted."
else
  show
  echo "Some resources remain; see above." >&2
  exit 1
fi
