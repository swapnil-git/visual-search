import json
import os


def load_secret(arn: str) -> dict:
    """Read a JSON secret from AWS Secrets Manager (RDS-style: username, password, host, port, dbname)."""
    import boto3
    return json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])
