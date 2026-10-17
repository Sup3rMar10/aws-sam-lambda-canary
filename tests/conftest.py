import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hooks"))


@pytest.fixture(autouse=True)
def fake_aws_environment(monkeypatch):
    """Dummy credentials and region so nothing can reach a real account, even by accident."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "us-east-1", "TABLE_NAME": "orders"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture()
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        t = ddb.create_table(
            TableName="orders", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        )
        yield t


def post_order(body, key=None, raw=None):
    headers = {"Idempotency-Key": key} if key else {}
    return {"httpMethod": "POST", "resource": "/orders", "headers": headers,
            "body": raw if raw is not None else json.dumps(body)}


def get_order_event(order_id):
    return {"httpMethod": "GET", "resource": "/orders/{id}", "pathParameters": {"id": order_id}}


GOOD = {"customer_id": "cust-1", "items": [{"sku": "A-1", "qty": 2}, {"sku": "B-2", "qty": 3}]}
