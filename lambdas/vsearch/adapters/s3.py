import os

from ..ports import ImageStore


class S3ImageStore(ImageStore):
    def __init__(self, bucket):
        import boto3
        self.bucket, self._c = bucket, boto3.client("s3")

    @classmethod
    def from_env(cls):
        return cls(os.environ["IMAGE_BUCKET"])

    def get(self, key):
        return self._c.get_object(Bucket=self.bucket, Key=key)["Body"].read()
