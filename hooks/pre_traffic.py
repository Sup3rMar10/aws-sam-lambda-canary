"""CodeDeploy PreTraffic hook: smoke-test the NEW Lambda version before it receives any traffic.

During a canary deployment CodeDeploy publishes the new version, then calls this function and waits for it to
report Succeeded or Failed. Failed stops the deployment before a single real request reaches the new code.
"""

from __future__ import annotations

import json
import os

import boto3


def smoke_test(lambda_client, function_arn: str) -> tuple[bool, str]:
    """Invoke the new version's health route. Anything but a 200 'ok' means do not shift traffic."""
    try:
        resp = lambda_client.invoke(FunctionName=function_arn, Payload=json.dumps({"httpMethod": "GET", "resource": "/health"}))
        payload = json.loads(resp["Payload"].read())
    except Exception as e:  # any failure to even call it is a failed check
        return False, f"could not invoke the new version: {type(e).__name__}: {e}"
    if resp.get("FunctionError"):
        return False, f"the new version raised an error: {payload}"
    if payload.get("statusCode") != 200 or json.loads(payload.get("body", "{}")).get("status") != "ok":
        return False, f"unexpected health response: {payload}"
    return True, "health check passed"


def handler(event, context=None, lambda_client=None, codedeploy_client=None):
    lambda_client = lambda_client or boto3.client("lambda")
    codedeploy = codedeploy_client or boto3.client("codedeploy")
    ok, message = smoke_test(lambda_client, os.environ["NEW_VERSION"])
    codedeploy.put_lifecycle_event_hook_execution_status(
        deploymentId=event["DeploymentId"],
        lifecycleEventHookExecutionId=event["LifecycleEventHookExecutionId"],
        status="Succeeded" if ok else "Failed",
    )
    return {"ok": ok, "message": message}
