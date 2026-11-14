#!/usr/bin/env bash
# Builds the PyMySQL Lambda layer (needed only for -c catalog_store=mysql). Run before `cdk deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf layers/pymysql && mkdir -p layers/pymysql/python
pip install -r lambdas/requirements-mysql.txt -t layers/pymysql/python --upgrade
echo "layer built at layers/pymysql"
