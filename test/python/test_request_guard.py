import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
# The module has no third-party imports, so it loads without LiteLLM or Switchyard installed.
_spec = importlib.util.spec_from_file_location(
    "request_guard", ROOT / "docker" / "gateway" / "switchyard_gateway" / "request_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PLAIN = {"model": "switchyard", "messages": [{"role": "user", "content": "Reply pong."}], "max_tokens": 8}
GROUP = "claude-opus-5-5"
BEARER = {"Authorization": "Bearer caller-value"}
PAIRS = [["Authorization", "Bearer caller-value"]]
CREDENTIALS = ["api_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"]
# A tool schema that describes an api_key parameter and an Authorization header: content, not a credential.
TOOL = {"type": "function", "function": {"name": "connect", "parameters": {"type": "object", "properties": {
    "api_key": {"type": "string"}, "headers": {"type": "object", "properties": {"Authorization": {"type": "string"}}}}}}}


class RequestGuardTests(unittest.TestCase):
    def test_plain_and_accepted_overrides_pass(self):
        self.assertIsNone(guard.credential_field(PLAIN))
        for body in [{"aws_region_name": "eu-west-1"}, {"model_id": "anthropic.claude-opus-5-5-v1:0"},
                     {"extra_headers": {"x-request-id": "abc"}}, {"headers": {"x-request-id": "abc"}},
                     {"headers": [["x-request-id", "abc"]]}, {"extra_headers": [["x-request-id", "abc"]]},
                     {"provider_specific_header": {"custom_llm_provider": "bedrock",
                                                   "extra_headers": {"x-request-id": "abc"}}},
                     {"provider_specific_header": [{"custom_llm_provider": ["bedrock"], "extra_headers": {}}]},
                     {"provider_specific_header": "not a mapping"}, {"provider_specific_header": None},
                     {"extra_body": {"temperature": 0}}, {"extra_body": "not a mapping"},
                     {"extra_headers": None}, {"headers": None},
                     # Fallback chains of model names, and the proxy's own metadata with the cleaned request headers.
                     {"fallbacks": [GROUP, {"switchyard": [GROUP]}], "context_window_fallbacks": [{GROUP: ["x"]}]},
                     {"metadata": {"tags": ["api_key"], "headers": {"x-request-id": "abc"}, "user_api_key": "hash"}},
                     # Credential names as values, not keys.
                     {"user": "api_key", "stop": ["api_key", "Authorization"], "extra_body": {"x": "api_key"}}]:
            self.assertIsNone(guard.credential_field({**PLAIN, **body}), body)

    def test_content_fields_may_describe_an_api_key_parameter(self):
        for body in [{"tools": [TOOL]}, {"functions": [TOOL["function"]]},
                     {"tool_choice": {"type": "function", "function": {"name": "connect", "api_key": "k"}}},
                     {"response_format": {"type": "json_schema", "json_schema": {"schema": {
                         "properties": {"api_key": {"type": "string"}, "extra_headers": BEARER}}}}},
                     {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "api_key": "k"}]}]},
                     {"extra_body": {"tools": [TOOL]}}, {"fallbacks": [{GROUP: [{"model": GROUP, "tools": [TOOL]}]}]}]:
            self.assertIsNone(guard.credential_field({**PLAIN, **body}), body)

    def test_each_credential_field_is_named_at_any_depth(self):
        for field in CREDENTIALS:
            self.assertEqual(guard.credential_field({**PLAIN, field: "value"}), field)
            self.assertEqual(guard.credential_field({**PLAIN, "extra_body": {field: "value"}}), "extra_body." + field)
            # LiteLLM merges a fallback dict target into the call with kwargs.update().
            target = {"model": GROUP, field: "value", "aws_region_name": "us-east-1"}
            self.assertEqual(guard.credential_field({**PLAIN, "fallbacks": [{GROUP: [target]}]}),
                             f"fallbacks[0].{GROUP}[0].{field}")
            self.assertEqual(guard.credential_field({**PLAIN, "fallbacks": [GROUP, {GROUP: ["x", target]}]}),
                             f"fallbacks[1].{GROUP}[1].{field}")
            self.assertEqual(guard.credential_field({**PLAIN, "context_window_fallbacks": [target]}),
                             "context_window_fallbacks[0]." + field)
            self.assertEqual(guard.credential_field({**PLAIN, "content_policy_fallbacks": [{GROUP: [target]}]}),
                             f"content_policy_fallbacks[0].{GROUP}[0].{field}")
            self.assertEqual(guard.credential_field({**PLAIN, "router_settings_override": {"fallbacks": [target]}}),
                             "router_settings_override.fallbacks[0]." + field)
            self.assertEqual(guard.credential_field({**PLAIN, "metadata": {field: "value"}}), "metadata." + field)
        self.assertEqual(guard.credential_field({**PLAIN, "api_key": None}), "api_key",
                         "presence counts, as in LiteLLM's own client-side credential check")
        self.assertEqual(guard.credential_field({**PLAIN, "aws_region_name": "us-east-1", "api_key": "k"}), "api_key")

    def test_authorization_in_a_header_container_is_named_in_any_case_and_shape(self):
        for container in ["extra_headers", "headers"]:
            for name in ["Authorization", "authorization", "AUTHORIZATION"]:
                self.assertEqual(guard.credential_field({**PLAIN, container: {name: "Bearer caller-value"}}),
                                 container + ".Authorization", name)
                # dict() turns a list of [name, value] pairs into the same header dict.
                self.assertEqual(guard.credential_field({**PLAIN, container: [[name, "Bearer caller-value"]]}),
                                 container + "[0].Authorization", name)
                self.assertEqual(guard.credential_field({**PLAIN, container: [["x-request-id", "abc"], (name, "v")]}),
                                 container + "[1].Authorization", name)
            # dict() reads a two-key mapping in a list as a (name, value) pair too.
            self.assertEqual(guard.credential_field({**PLAIN, container: [{"Authorization": 1, "Bearer caller": 2}]}),
                             container + "[0].Authorization")
            self.assertEqual(guard.credential_field({**PLAIN, "extra_body": {container: BEARER}}),
                             "extra_body." + container + ".Authorization")
            self.assertEqual(guard.credential_field({**PLAIN, "fallbacks": [{GROUP: [{"model": GROUP, container: PAIRS}]}]}),
                             f"fallbacks[0].{GROUP}[0].{container}[0].Authorization")
        self.assertEqual(guard.credential_field({**PLAIN, "headers": {"x-request-id": "abc"}, "extra_headers": PAIRS}),
                         "extra_headers[0].Authorization")

    def test_authorization_in_provider_specific_header_is_named(self):
        one = {"custom_llm_provider": "bedrock", "extra_headers": {"authorization": "Bearer caller-value"}}
        other = {"custom_llm_provider": "openai", "extra_headers": {"x-request-id": "abc"}}
        prefix = "provider_specific_header"
        for value, expected in [(one, prefix + ".extra_headers.Authorization"),
                                ([one], prefix + "[0].extra_headers.Authorization"),
                                ([other, one], prefix + "[1].extra_headers.Authorization"),
                                ([{"extra_headers": PAIRS}], prefix + "[0].extra_headers[0].Authorization"),
                                ({"Authorization": "Bearer caller-value"}, prefix + ".Authorization")]:
            self.assertEqual(guard.credential_field({**PLAIN, prefix: value}), expected, value)
        self.assertEqual(guard.credential_field({**PLAIN, "extra_body": {prefix: [one]}}),
                         "extra_body." + prefix + "[0].extra_headers.Authorization")

    def test_a_deep_body_is_walked_without_recursion(self):
        clean: object = "value"
        hidden: object = {"api_key": "value"}
        for _ in range(5000):
            clean, hidden = {"x": [clean]}, {"x": [hidden]}
        self.assertIsNone(guard.credential_field({**PLAIN, "extra_body": clean}))
        found = guard.credential_field({**PLAIN, "extra_body": hidden})
        self.assertTrue(found.startswith("extra_body.x[0].x[0].") and found.endswith(".api_key"), found[-40:])

    def test_rejection_message_names_the_field(self):
        message = guard.REJECTION.format(field="fallbacks[0].claude-opus-5-5[0].api_key")
        self.assertTrue(message.startswith("Rejected Request: fallbacks[0].claude-opus-5-5[0].api_key is not accepted"),
                        message)
        self.assertIn("task role", message)


if __name__ == "__main__":
    unittest.main()
