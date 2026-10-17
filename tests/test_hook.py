import io
import json

import pytest

import pre_traffic


class FakeLambda:
    def __init__(self, payload=None, function_error=None, raises=None):
        self.payload, self.function_error, self.raises = payload, function_error, raises
        self.calls = []

    def invoke(self, FunctionName, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        if self.raises:
            raise self.raises
        out = {"Payload": io.BytesIO(json.dumps(self.payload).encode())}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


class FakeCodeDeploy:
    def __init__(self):
        self.reports = []

    def put_lifecycle_event_hook_execution_status(self, **kw):
        self.reports.append(kw)


HEALTHY = {"statusCode": 200, "body": json.dumps({"status": "ok", "version": "2"})}
EVENT = {"DeploymentId": "d-123", "LifecycleEventHookExecutionId": "hook-9"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NEW_VERSION", "arn:aws:lambda:us-east-1:123456789012:function:orders:7")


def run(fake_lambda):
    cd = FakeCodeDeploy()
    result = pre_traffic.handler(EVENT, lambda_client=fake_lambda, codedeploy_client=cd)
    return result, cd


def test_a_healthy_new_version_is_reported_as_succeeded():
    fl = FakeLambda(HEALTHY)
    result, cd = run(fl)
    assert result["ok"] is True
    assert cd.reports == [{"deploymentId": "d-123", "lifecycleEventHookExecutionId": "hook-9", "status": "Succeeded"}]


def test_the_smoke_test_calls_the_new_version_not_the_alias():
    fl = FakeLambda(HEALTHY)
    run(fl)
    assert fl.calls == [("arn:aws:lambda:us-east-1:123456789012:function:orders:7", {"httpMethod": "GET", "resource": "/health"})]


@pytest.mark.parametrize(
    "fake",
    [
        FakeLambda({"statusCode": 500, "body": "{}"}),
        FakeLambda({"statusCode": 200, "body": json.dumps({"status": "degraded"})}),
        FakeLambda({"errorMessage": "boom"}, function_error="Unhandled"),
        FakeLambda(raises=RuntimeError("throttled")),
        FakeLambda({"statusCode": 200}),
    ],
    ids=["http 500", "status not ok", "function error", "invoke fails", "no body"],
)
def test_any_unhealthy_response_blocks_the_traffic_shift(fake):
    result, cd = run(fake)
    assert result["ok"] is False
    assert [r["status"] for r in cd.reports] == ["Failed"]


def test_exactly_one_status_is_reported_per_run():
    _, cd = run(FakeLambda(HEALTHY))
    assert len(cd.reports) == 1
