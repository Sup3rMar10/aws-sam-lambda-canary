# aws-sam-lambda-canary

![ci](https://github.com/Sup3rMar10/aws-sam-lambda-canary/actions/workflows/ci.yml/badge.svg)

A serverless orders API (API Gateway, Lambda, DynamoDB) deployed with AWS SAM, built around **safe deployments**: prod shifts 10% of traffic to the new version first, a **pre-traffic hook** smoke-tests the new code before it gets any, and a CloudWatch **errors alarm rolls the deployment back** automatically. The business code is small on purpose; the delivery is the point.

> **What is verified, and what is not.** 46 tests pass locally on Python 3.13 against **moto**, an in-memory emulation of DynamoDB (no AWS account, dummy credentials), and `cfn-lint` passes on the SAM template. In GitHub Actions the tests also pass on Python 3.11 and 3.13, `sam validate --lint` and `sam build` pass, and `sam local invoke` ran the **built** function inside the Lambda runtime image (arm64, emulated): `/health` returned 200 and an invalid order returned 400 (2026-10-17). **Nothing here has ever been deployed to AWS**: the canary, the hook running under CodeDeploy and the alarm rollback are configured and unit-tested but have never executed. Emulation is not AWS (see limits). The deploy workflow's jobs are skipped unless you set `AWS_DEPLOY_ROLE_ARN`.

## What is in it

```
src/app.py          the API: POST /orders (idempotent), GET /orders/{id}, GET /health
hooks/pre_traffic.py  CodeDeploy hook: invokes the NEW version's /health before any traffic shifts
template.yaml       SAM: table, API with a key and throttling, function with canary + alarm + hook
tests/              handler tests (moto), hook tests (fake clients), template tests
events/             sample events for `sam local invoke`
```

## Safe deployment, as configured

| Stage | What happens | Where it is set |
|---|---|---|
| dev | `AllAtOnce`: replace the code immediately | `DeploymentPreference.Type` is `!If [IsProd, Canary10Percent5Minutes, AllAtOnce]` |
| prod, step 1 | CodeDeploy publishes the new version; the **pre-traffic hook** invokes it on `/health` and reports Succeeded or Failed. A failure stops the deploy with **zero** real requests on the new code | `Hooks.PreTraffic` |
| prod, step 2 | 10% of traffic to the new version for 5 minutes on the `live` alias | `Canary10Percent5Minutes`, `AutoPublishAlias: live` |
| prod, rollback | if the `Errors` alarm on the live alias fires during the shift, CodeDeploy rolls back | `Alarms`, `TreatMissingData: notBreaching` (no traffic is not an error) |

The template tests assert each of these, including that the alarm watches the `live` alias and not the bare function, that the hook's name starts with `CodeDeployHook_` (CodeDeploy only invokes functions with that prefix), and that the hook itself is not canary-deployed.

## The API's idempotency

Clients retry, so with an `Idempotency-Key` header the order id is derived from the key and the write is **conditional** (`attribute_not_exists`):

| Request | Result |
|---|---|
| first POST with a key | 201, order created |
| retry, same key, same body | 200 with `Idempotent-Replay: true`, the **original** order, no duplicate |
| same key, different body | 409 (a conflict, never a silent overwrite) |
| 10 concurrent retries with one key | exactly one 201 and nine 200s, one row in the table (tested with threads) |
| no key | a new order every time |

Validation reports **all** problems at once (13 invalid-body cases tested, including `qty: true`, `qty: 1.5`, `qty: "2"` and 51 items), and a rejected request writes nothing.

## Honest limits

- **Never deployed.** The canary, the CodeDeploy hook and the alarm rollback have never run. In particular I have not verified the hook's IAM permissions work under a real deployment, or that the alarm's dimensions match what Lambda emits for the alias.
- **moto is not DynamoDB.** It emulates the API well enough to test the logic, including the conditional write and the concurrency race, but it does not model throttling, consistency or latency. A passing test here is not a performance or scale claim.
- The hook is tested with fake clients, not a real CodeDeploy callback.
- The smoke test is only `/health`; it does not exercise DynamoDB, so a broken table permission would pass the hook and be caught by the alarm during the canary (which is what the alarm is for).
- The API uses an API key with a usage plan, which is a throttling and identification mechanism, not strong authentication; a real service needs an authorizer (IAM, Cognito or a Lambda authorizer).
- The table has `DeletionPolicy: Retain`, so deleting the stack leaves the data (and a table that must be removed by hand).
- No custom domain, WAF, structured logging or metrics beyond tracing and the one alarm.

## Run it

```
python -m pip install pytest moto boto3 pyyaml cfn-lint
python -m pytest
cfn-lint template.yaml
# with the SAM CLI and Docker:
sam validate --lint && sam build
sam local invoke OrdersFunction --event events/health.json
```
