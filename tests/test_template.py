"""The safe-deployment settings are the point of this repo, so the template is tested for them."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {("Ref" if suffix == "Ref" else f"Fn::{suffix}"): value}


_Loader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def t():
    return yaml.load((ROOT / "template.yaml").read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506


def res(t, name):
    return t["Resources"][name]["Properties"]


def test_prod_shifts_ten_percent_of_traffic_first_and_dev_deploys_at_once(t):
    pref = res(t, "OrdersFunction")["DeploymentPreference"]
    assert pref["Type"] == {"Fn::If": ["IsProd", "Canary10Percent5Minutes", "AllAtOnce"]}
    assert t["Conditions"]["IsProd"] == {"Fn::Equals": [{"Ref": "Stage"}, "prod"]}


def test_the_function_publishes_a_live_alias_for_codedeploy_to_shift(t):
    assert res(t, "OrdersFunction")["AutoPublishAlias"] == "live"


def test_an_errors_alarm_is_wired_to_roll_the_deployment_back(t):
    pref = res(t, "OrdersFunction")["DeploymentPreference"]
    assert pref["Alarms"] == [{"Ref": "OrdersErrorsAlarm"}]
    alarm = res(t, "OrdersErrorsAlarm")
    assert alarm["MetricName"] == "Errors" and alarm["Namespace"] == "AWS/Lambda"
    assert alarm["Threshold"] == 0 and alarm["ComparisonOperator"] == "GreaterThanThreshold"
    resource_dim = next(d for d in alarm["Dimensions"] if d["Name"] == "Resource")
    assert resource_dim["Value"]["Fn::Sub"].endswith(":live"), "the alarm must watch the live alias, not the bare function"
    assert alarm["TreatMissingData"] == "notBreaching", "no traffic is not an error"


def test_a_pre_traffic_hook_smoke_tests_the_new_version(t):
    assert res(t, "OrdersFunction")["DeploymentPreference"]["Hooks"]["PreTraffic"] == {"Ref": "PreTrafficFunction"}
    hook = res(t, "PreTrafficFunction")
    assert hook["FunctionName"]["Fn::Sub"].startswith("CodeDeployHook_"), "CodeDeploy only invokes functions with this prefix"
    assert hook["Environment"]["Variables"]["NEW_VERSION"] == {"Ref": "OrdersFunction.Version"}
    assert hook["DeploymentPreference"] == {"Enabled": False}, "the hook itself must not be canary-deployed"


def test_the_table_is_encrypted_recoverable_and_survives_stack_deletion(t):
    table = t["Resources"]["OrdersTable"]
    assert table["DeletionPolicy"] == "Retain" and table["UpdateReplacePolicy"] == "Retain"
    p = table["Properties"]
    assert p["SSESpecification"]["SSEEnabled"] is True
    assert p["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert p["BillingMode"] == "PAY_PER_REQUEST"


def test_the_api_requires_a_key_and_is_throttled(t):
    auth = res(t, "OrdersApi")["Auth"]
    assert auth["ApiKeyRequired"] is True
    assert auth["UsagePlan"]["Throttle"]["RateLimit"] > 0


def test_functions_use_a_current_runtime_arm_and_tracing(t):
    g = t["Globals"]["Function"]
    assert g["Runtime"] == "python3.13" and g["Architectures"] == ["arm64"] and g["Tracing"] == "Active"


def test_no_iam_statement_uses_a_wildcard_resource(t):
    for name, r in t["Resources"].items():
        for pol in r.get("Properties", {}).get("Policies", []):
            if isinstance(pol, dict) and "Statement" in pol:
                for stmt in pol["Statement"]:
                    assert stmt["Resource"] != "*", f"{name} grants on '*'"


def test_the_code_the_template_points_at_exists_with_the_named_handlers():
    import app
    import pre_traffic

    assert callable(app.handler) and callable(pre_traffic.handler)
    assert (ROOT / "src" / "app.py").exists() and (ROOT / "hooks" / "pre_traffic.py").exists()
    assert "TABLE_NAME" in (ROOT / "src" / "app.py").read_text(encoding="utf-8")
