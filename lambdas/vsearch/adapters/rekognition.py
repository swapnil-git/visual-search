from ..ports import TextReader


class RekognitionReader(TextReader):
    def __init__(self):
        import boto3
        self._c = boto3.client("rekognition")

    @classmethod
    def from_env(cls):
        return cls()

    def read_lines(self, image):
        r = self._c.detect_text(Image={"Bytes": image})
        return [(d["DetectedText"], d.get("Confidence", 0))
                for d in r.get("TextDetections", []) if d.get("Type") == "LINE"]
