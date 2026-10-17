"""An orders API on Lambda + DynamoDB: create an order (idempotently) and fetch it.

Idempotency matters because clients retry. With an Idempotency-Key header the order id is derived from the
key, and the write is conditional, so a retry returns the original order instead of creating a second one.
The same key with a different body is a conflict, not a silent overwrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

MAX_ITEMS = 50
MAX_QTY = 1000
VERSION = os.environ.get("APP_VERSION", "dev")


def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def _response(status: int, body: dict, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=lambda o: int(o) if isinstance(o, Decimal) and o == o.to_integral() else float(o)),
    }


def validate_order(body) -> list[str]:
    """Every problem with the request body, so the caller can fix them all at once."""
    if not isinstance(body, dict):
        return ["body must be a JSON object"]
    problems = []
    cid = body.get("customer_id")
    if not isinstance(cid, str) or not cid.strip():
        problems.append("customer_id must be a non-empty string")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        problems.append("items must be a non-empty list")
    elif len(items) > MAX_ITEMS:
        problems.append(f"items may hold at most {MAX_ITEMS} entries")
    else:
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("sku"), str) or not item["sku"].strip():
                problems.append(f"items[{i}].sku must be a non-empty string")
            qty = item.get("qty") if isinstance(item, dict) else None
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
                problems.append(f"items[{i}].qty must be an integer from 1 to {MAX_QTY}")
    return problems


def _fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def create_order(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "")
    except ValueError:
        return _response(400, {"error": "body is not valid JSON"})
    problems = validate_order(body)
    if problems:
        return _response(400, {"error": "invalid order", "problems": problems})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    key = headers.get("idempotency-key")
    order_id = hashlib.sha256(key.encode()).hexdigest()[:20] if key else uuid.uuid4().hex[:20]
    item = {
        "order_id": order_id, "customer_id": body["customer_id"], "items": body["items"],
        "total_qty": sum(i["qty"] for i in body["items"]), "status": "created", "fingerprint": _fingerprint(body),
    }
    table = _table()
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(order_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"order_id": order_id}).get("Item", {})
        if existing.get("fingerprint") != item["fingerprint"]:
            return _response(409, {"error": "this Idempotency-Key was already used with a different request"})
        return _response(200, {"order_id": order_id, "status": existing["status"], "total_qty": existing["total_qty"]},
                         {"Idempotent-Replay": "true"})
    return _response(201, {"order_id": order_id, "status": "created", "total_qty": item["total_qty"]})


def get_order(event: dict) -> dict:
    order_id = (event.get("pathParameters") or {}).get("id", "")
    if not order_id:
        return _response(400, {"error": "missing order id"})
    item = _table().get_item(Key={"order_id": order_id}).get("Item")
    if not item:
        return _response(404, {"error": "order not found"})
    item.pop("fingerprint", None)
    return _response(200, item)


def handler(event, context=None):
    method, resource = event.get("httpMethod"), event.get("resource")
    if (method, resource) == ("GET", "/health"):
        return _response(200, {"status": "ok", "version": VERSION})
    if (method, resource) == ("POST", "/orders"):
        return create_order(event)
    if (method, resource) == ("GET", "/orders/{id}"):
        return get_order(event)
    return _response(404, {"error": "no such route"})
