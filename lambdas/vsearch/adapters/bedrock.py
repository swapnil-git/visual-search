"""Amazon Bedrock Titan Multimodal Embeddings. Swap models by changing EMBED_MODEL_ID / EMBED_DIM
(and this request body if the model's schema differs), then re-index."""
import base64
import json
import math
import os

from ..ports import Embedder


class BedrockTitanEmbedder(Embedder):
    def __init__(self, model_id="amazon.titan-embed-image-v1", dim=1024, region=None):
        import boto3
        self.model_id, self.dim = model_id, dim
        self._client = boto3.client("bedrock-runtime", region_name=region)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-image-v1"),
                   int(os.environ.get("EMBED_DIM", "1024")), os.environ.get("AWS_REGION"))

    def embed_image(self, image):
        body = {"inputImage": base64.b64encode(image).decode(),
                "embeddingConfig": {"outputEmbeddingLength": self.dim}}
        resp = self._client.invoke_model(modelId=self.model_id, body=json.dumps(body),
                                         accept="application/json", contentType="application/json")
        vec = json.loads(resp["body"].read())["embedding"]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]  # unit length
