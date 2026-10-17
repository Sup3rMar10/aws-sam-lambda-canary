import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import app
from conftest import GOOD, get_order_event, post_order


def body(resp):
    return json.loads(resp["body"])


def test_health_route():
    resp = app.handler({"httpMethod": "GET", "resource": "/health"})
    assert resp["statusCode"] == 200 and body(resp)["status"] == "ok"


def test_an_unknown_route_is_a_404():
    assert app.handler({"httpMethod": "DELETE", "resource": "/orders"})["statusCode"] == 404


def test_create_then_fetch(table):
    created = app.handler(post_order(GOOD))
    assert created["statusCode"] == 201
    out = body(created)
    assert out["total_qty"] == 5 and out["status"] == "created"
    fetched = app.handler(get_order_event(out["order_id"]))
    assert fetched["statusCode"] == 200
    order = body(fetched)
    assert order["customer_id"] == "cust-1" and order["total_qty"] == 5
    assert "fingerprint" not in order, "internal bookkeeping must not leak to callers"
    assert isinstance(order["items"][0]["qty"], int), "DynamoDB's Decimal must come back as a plain number"


def test_without_an_idempotency_key_every_post_is_a_new_order(table):
    a, b = body(app.handler(post_order(GOOD)))["order_id"], body(app.handler(post_order(GOOD)))["order_id"]
    assert a != b and table.scan()["Count"] == 2


def test_a_retry_with_the_same_key_returns_the_original_order(table):
    first = app.handler(post_order(GOOD, key="abc-123"))
    retry = app.handler(post_order(GOOD, key="abc-123"))
    assert first["statusCode"] == 201 and retry["statusCode"] == 200
    assert body(first)["order_id"] == body(retry)["order_id"]
    assert retry["headers"]["Idempotent-Replay"] == "true"
    assert table.scan()["Count"] == 1


def test_the_same_key_with_a_different_body_is_a_conflict_not_an_overwrite(table):
    app.handler(post_order(GOOD, key="k1"))
    other = {"customer_id": "cust-2", "items": [{"sku": "Z", "qty": 1}]}
    resp = app.handler(post_order(other, key="k1"))
    assert resp["statusCode"] == 409
    stored = table.scan()["Items"]
    assert len(stored) == 1 and stored[0]["customer_id"] == "cust-1"


def test_the_idempotency_header_name_is_case_insensitive(table):
    event = post_order(GOOD)
    event["headers"] = {"idempotency-key": "lower"}
    again = post_order(GOOD)
    again["headers"] = {"IDEMPOTENCY-KEY": "lower"}
    assert app.handler(event)["statusCode"] == 201 and app.handler(again)["statusCode"] == 200


def test_ten_concurrent_retries_create_exactly_one_order(table):
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: app.handler(post_order(GOOD, key="race")), range(10)))
    codes = sorted(r["statusCode"] for r in results)
    assert codes.count(201) == 1 and codes.count(200) == 9
    assert table.scan()["Count"] == 1


def test_missing_and_unknown_orders(table):
    assert app.handler(get_order_event("nope"))["statusCode"] == 404
    assert app.handler({"httpMethod": "GET", "resource": "/orders/{id}", "pathParameters": None})["statusCode"] == 400


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null", '"text"'])
def test_garbage_bodies_are_400_and_write_nothing(table, raw):
    resp = app.handler(post_order(None, raw=raw))
    assert resp["statusCode"] == 400 and table.scan()["Count"] == 0


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ({"items": [{"sku": "A", "qty": 1}]}, "customer_id"),
        ({"customer_id": "  ", "items": [{"sku": "A", "qty": 1}]}, "customer_id"),
        ({"customer_id": "c"}, "items must be"),
        ({"customer_id": "c", "items": []}, "items must be"),
        ({"customer_id": "c", "items": [{"qty": 1}]}, "sku"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": 0}]}, "qty"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": -3}]}, "qty"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": 1001}]}, "qty"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": 1.5}]}, "qty"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": True}]}, "qty"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": "2"}]}, "qty"),
        ({"customer_id": "c", "items": ["A"]}, "sku"),
        ({"customer_id": "c", "items": [{"sku": "A", "qty": 1}] * 51}, "at most 50"),
    ],
)
def test_invalid_orders_are_rejected_with_a_reason(table, bad, fragment):
    resp = app.handler(post_order(bad))
    assert resp["statusCode"] == 400
    assert fragment in " ".join(body(resp)["problems"])
    assert table.scan()["Count"] == 0


def test_all_problems_are_reported_together():
    problems = app.validate_order({"items": [{"sku": "", "qty": 0}]})
    assert len(problems) == 3  # customer_id, sku, qty


def test_the_limits_are_inclusive(table):
    ok = {"customer_id": "c", "items": [{"sku": f"S{i}", "qty": 1000} for i in range(50)]}
    assert app.handler(post_order(ok))["statusCode"] == 201
