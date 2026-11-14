import json
from decimal import Decimal


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def response(status: int, payload: dict):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, cls=DecimalEncoder)}
