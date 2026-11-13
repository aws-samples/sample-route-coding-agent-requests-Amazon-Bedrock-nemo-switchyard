import contextlib
import importlib.util
import io
import urllib.error
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("smoke_test", ROOT / "scripts" / "smoke-test.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


class SmokeClosedPathTests(unittest.TestCase):
    def test_local_gateway_urls_are_recognized(self):
        for url in ["http://localhost:4000", "http://127.0.0.1:4000/", "http://[::1]:4000",
                    "http://host.docker.internal:4000", "http://LOCALHOST"]:
            self.assertTrue(smoke.is_local_gateway(url), url)
        for url in ["http://gw-123.us-east-1.elb.amazonaws.com", "https://llm-gateway.example.com"]:
            self.assertFalse(smoke.is_local_gateway(url), url)

    def test_closed_paths_cover_admin_docs_and_billable_health(self):
        self.assertEqual(smoke.CLOSED_PATHS, ["/ui", "/redoc", "/openapi.json", "/key/list",
                                              "/sso/key/generate", "/health", "/health/readiness"])

    def test_remote_gateway_must_answer_load_balancer_404_on_every_closed_path(self):
        seen = []

        def fetch(url, path):
            seen.append(path)
            return 404, "Not found"

        with contextlib.redirect_stdout(io.StringIO()):
            smoke.check_closed_paths("https://llm-gateway.example.com", fetch)
        self.assertEqual(seen, smoke.CLOSED_PATHS)

    def test_remote_gateway_fails_when_a_closed_path_reaches_litellm(self):
        responses = {"/redoc": (200, "<html>ReDoc</html>")}

        def fetch(url, path):
            return responses.get(path, (404, "Not found"))

        with self.assertRaisesRegex(AssertionError, "/redoc"):
            smoke.check_closed_paths("https://llm-gateway.example.com", fetch)

    def test_litellm_404_is_not_mistaken_for_the_load_balancer(self):
        def fetch(url, path):
            return 404, '{"detail":"Not Found"}'

        with self.assertRaisesRegex(AssertionError, "load balancer"):
            smoke.check_closed_paths("https://llm-gateway.example.com", fetch)

    def test_local_gateway_skips_closed_path_checks(self):
        def fetch(url, path):
            raise AssertionError("must not be called for a local gateway")

        with contextlib.redirect_stdout(io.StringIO()):
            smoke.check_closed_paths("http://127.0.0.1:4000", fetch)

    def test_unreachable_gateway_reports_a_clear_failure(self):
        def refuse(url, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

        original = smoke.urllib.request.urlopen
        smoke.urllib.request.urlopen = refuse
        try:
            with self.assertRaisesRegex(AssertionError, "/ui: gateway unreachable"):
                smoke.check_closed_paths("https://llm-gateway.example.com")
        finally:
            smoke.urllib.request.urlopen = original


class SmokeGatewaySourceTests(unittest.TestCase):
    def stack(self, calls):
        def read(stack_name):
            calls.append(stack_name)
            return "https://stack.example.com", "sk-from-stack"
        return read

    def test_url_and_key_from_the_environment_skip_the_stack(self):
        calls = []
        url, key, source = smoke.resolve_gateway(
            {"GATEWAY_URL": "http://127.0.0.1:4000", "LITELLM_MASTER_KEY": "sk-local"},
            self.stack(calls))
        self.assertEqual((url, key), ("http://127.0.0.1:4000", "sk-local"))
        self.assertEqual(calls, [])
        self.assertIn("environment", source)

    def test_url_from_the_environment_without_a_key_reads_the_key_from_the_stack(self):
        calls = []
        url, key, source = smoke.resolve_gateway({"GATEWAY_URL": "http://127.0.0.1:4000"},
                                                 self.stack(calls))
        self.assertEqual((url, key), ("http://127.0.0.1:4000", "sk-from-stack"))
        self.assertEqual(calls, ["SwitchyardGateway"])
        self.assertIn("SwitchyardGateway", source)

    def test_without_a_url_an_exported_key_is_ignored(self):
        calls = []
        url, key, source = smoke.resolve_gateway(
            {"LITELLM_MASTER_KEY": "sk-local", "STACK_NAME": "MyGateway"}, self.stack(calls))
        self.assertEqual((url, key), ("https://stack.example.com", "sk-from-stack"))
        self.assertEqual(calls, ["MyGateway"])
        self.assertIn("MyGateway", source)
        self.assertIn("ignored", source)


class SmokeInputFormatTests(unittest.TestCase):
    GOOD = {
        "AWS Region": ["us-east-1", "eu-central-1", "ap-southeast-2", "us-gov-west-1"],
        "STACK_NAME": ["SwitchyardGateway", "My-Gateway-2", "a" * 128],
        "ROUTING_PROFILE": ["stage", "random", "my-profile-2"],
        "MasterKeySecretArn": [
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:SwitchyardGatewayMasterKey-AbCdEf",
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:team/key_1+=.@-x"],
    }
    BAD = {
        "AWS Region": ["", "US-EAST-1", "us-east-1 ", "us-east-1\n", "us-east-1;id", "--region"],
        "STACK_NAME": ["", "1stack", "-stack", "my stack", "stack;id", "a" * 129,
                       "arn:aws:cloudformation:us-east-1:123456789012:stack/SwitchyardGateway/id"],
        "ROUTING_PROFILE": ["", "Stage", "../stage", "stage profile", "-x"],
        "MasterKeySecretArn": [
            "", "--secret-id", "arn:aws:s3:::bucket", "arn:aws:secretsmanager:us-east-1:12345:secret:x",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:x;id"],
    }

    def test_values_in_the_expected_format_pass_through(self):
        for setting, values in self.GOOD.items():
            for value in values:
                self.assertEqual(smoke.validated(setting, value), value, setting)

    def test_other_values_are_rejected_with_the_setting_name(self):
        for setting, values in self.BAD.items():
            for value in values:
                with self.assertRaisesRegex(ValueError, setting, msg=f"{setting}={value!r}"):
                    smoke.validated(setting, value)

    def test_invalid_region_stops_before_the_aws_cli_runs(self):
        with mock.patch.dict(smoke.os.environ, {"AWS_REGION": "us-east-1 --profile admin"}), \
                mock.patch.object(smoke, "executable", return_value="/usr/local/bin/aws"), \
                mock.patch.object(smoke.subprocess, "check_output") as check_output:
            with self.assertRaisesRegex(ValueError, "AWS Region"):
                smoke.aws("sts", "get-caller-identity")
        check_output.assert_not_called()

    def test_a_command_missing_from_path_is_a_clear_failure(self):
        with mock.patch.object(smoke.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "aws is not on PATH"):
                smoke.executable("aws")


class SmokeGroupTests(unittest.TestCase):
    MANIFEST = {
        "modelGroup": "switchyard",
        "models": ["bedrock/converse/global.anthropic.claude-opus-5-5",
                   "bedrock/converse/nvidia.nemotron-super-3-120b"],
        "baselines": {"claude-opus-5-5": "bedrock/converse/global.anthropic.claude-opus-5-5",
                      "nemotron-super-3-120b": "bedrock/converse/nvidia.nemotron-super-3-120b"},
    }

    def test_every_baseline_group_gets_one_request_and_answers_with_its_model(self):
        asked = []

        def select(group):
            asked.append(group)
            return self.MANIFEST["baselines"][group]

        with contextlib.redirect_stdout(io.StringIO()):
            smoke.check_baselines(self.MANIFEST, select)
        self.assertEqual(asked, ["claude-opus-5-5", "nemotron-super-3-120b"])

    def test_baseline_answered_by_another_model_fails(self):
        def select(group):
            return "global.anthropic.claude-opus-5-5"

        with self.assertRaisesRegex(AssertionError, "nemotron-super-3-120b"):
            with contextlib.redirect_stdout(io.StringIO()):
                smoke.check_baselines(self.MANIFEST, select)

    def test_header_prefixes_do_not_matter(self):
        def select(group):
            return self.MANIFEST["baselines"][group].removeprefix("bedrock/converse/")

        with contextlib.redirect_stdout(io.StringIO()):
            smoke.check_baselines(self.MANIFEST, select)


if __name__ == "__main__":
    unittest.main()
