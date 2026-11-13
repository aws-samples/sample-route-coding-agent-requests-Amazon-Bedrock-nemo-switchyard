"""Route through the real profile files with mock responses: no AWS calls.

Run inside the image, with the repository mounted read-only and no port published:

  docker run --rm --network none --entrypoint python \
    -e REPO_ROOT=/repo -e AWS_REGION=us-east-1 -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD:/repo:ro" switchyard-litellm:ci \
    -m unittest discover -s /repo/docker/gateway/tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path

import litellm
import yaml
from fastapi import HTTPException
from litellm import Router
from litellm.caching.caching import DualCache
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.proxy._types import UserAPIKeyAuth
from switchyard_litellm import StageRoutingPlugin
from switchyard_litellm.configuration.loader import load_routing_plugin
from switchyard_litellm.plugins.random_routing_plugin import RandomRoutingPlugin

from switchyard_gateway import MultiCandidateRouting

REPO = Path(os.environ.get("REPO_ROOT", "/repo"))
PROFILES = REPO / "profiles"
FIXTURE = REPO / "scripts" / "fixtures" / "stage-escalation.json"
PLUGIN = "switchyard_gateway.configured.ROUTING_PLUGIN"
ROUTED = "switchyard"
REPEATS = 10
PLAIN = [{"role": "user", "content": "Reply with pong."}]


def load_profile(name: str) -> dict:
    return yaml.safe_load((PROFILES / name / "litellm.yaml").read_text())


def mock_model_list(doc: dict) -> list[dict]:
    """The profile's model list with every provider call replaced by a mock reply."""
    out = []
    for entry in doc["model_list"]:
        params = {k: v for k, v in entry["litellm_params"].items() if k != "aws_region_name"}
        params["mock_response"] = f"served by {params['model']}"
        out.append({"model_name": entry["model_name"], "litellm_params": params})
    return out


def group(doc: dict, name: str) -> list[str]:
    return [e["litellm_params"]["model"] for e in doc["model_list"] if e["model_name"] == name]


def served(response) -> str:
    return response.choices[0].message.content.removeprefix("served by ")


class ProfileRouter(unittest.IsolatedAsyncioTestCase):
    """One event loop per test, so LiteLLM's logging worker never changes loops."""

    profile = ""

    def setUp(self) -> None:
        self.doc = load_profile(self.profile)
        self._callbacks = list(litellm.callbacks)
        self.router = self.routed_by(
            MultiCandidateRouting(load_routing_plugin(PROFILES / self.profile / "switchyard.toml"))
        )

    async def asyncTearDown(self) -> None:
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=30)
        litellm.callbacks[:] = self._callbacks

    def routed_by(self, plugin) -> Router:
        """A Router over the profile's mock model list, with `plugin` registered in both places."""
        litellm.callbacks.append(plugin)
        return Router(model_list=mock_model_list(self.doc), plugins=[plugin])

    async def ask(self, model: str, messages: list[dict], router: Router | None = None) -> str:
        return served(await (router or self.router).acompletion(model=model, messages=messages))

    def baselines(self) -> list[str]:
        return sorted({e["model_name"] for e in self.doc["model_list"]} - {ROUTED})


class ProfileFiles(unittest.TestCase):
    def test_every_profile_registers_the_wrapper_and_loads(self) -> None:
        for name in ("stage", "random"):
            doc = load_profile(name)
            self.assertEqual(doc["router_settings"]["plugins"], [PLUGIN], name)
            self.assertIn(PLUGIN, doc["litellm_settings"]["callbacks"], name)
            load_routing_plugin(PROFILES / name / "switchyard.toml")


class StageProfile(ProfileRouter):
    profile = "stage"

    async def test_plain_prompt_goes_to_the_efficient_model(self) -> None:
        _, efficient = group(self.doc, ROUTED)
        picks = {await self.ask(ROUTED, PLAIN) for _ in range(REPEATS)}
        self.assertEqual(picks, {efficient})

    async def test_repeated_failures_escalate_to_the_capable_model(self) -> None:
        capable, _ = group(self.doc, ROUTED)
        messages = json.loads(FIXTURE.read_text())["messages"]
        picks = {await self.ask(ROUTED, messages) for _ in range(REPEATS)}
        self.assertEqual(picks, {capable})

    async def test_baseline_groups_pass_through_to_their_one_model(self) -> None:
        for name in self.baselines():
            (model,) = group(self.doc, name)
            self.assertEqual(await self.ask(name, [{"role": "user", "content": "hi"}]), model)

    async def test_without_the_wrapper_stage_rejects_a_baseline_group(self) -> None:
        raw = StageRoutingPlugin(picker="efficient_first", confidence_threshold=0.5)
        router = Router(model_list=mock_model_list(self.doc), plugins=[raw])
        with self.assertRaisesRegex(ValueError, "exactly two unique"):
            await router.acompletion(model=self.baselines()[0], messages=[{"role": "user", "content": "hi"}])


class RequestBodyCredentials(unittest.IsolatedAsyncioTestCase):
    """The wrapper's proxy pre-call hook rejects caller credentials before routing."""

    def setUp(self) -> None:
        self.plugin = MultiCandidateRouting(load_routing_plugin(PROFILES / "stage" / "switchyard.toml"))

    async def hook(self, body: dict):
        return await self.plugin.async_pre_call_hook(
            user_api_key_dict=UserAPIKeyAuth(),
            cache=DualCache(),
            data={"model": ROUTED, "messages": PLAIN, **body},
            call_type="completion",
        )

    def test_the_proxy_dispatches_the_hook(self) -> None:
        # The proxy runs async_pre_call_hook only when the registered object's own class defines it.
        self.assertIn("async_pre_call_hook", vars(MultiCandidateRouting))

    async def test_plain_requests_and_accepted_overrides_pass(self) -> None:
        self.assertIsNone(await self.hook({}))
        self.assertIsNone(await self.hook({"aws_region_name": "us-east-1", "extra_headers": {"x-id": "1"}}))
        self.assertIsNone(await self.hook({"headers": {"x-id": "1"}, "provider_specific_header": [
            {"custom_llm_provider": "bedrock", "extra_headers": {"x-id": "1"}}]}))
        # Fallback chains of model names, and a tool schema that describes an api_key parameter, are content.
        self.assertIsNone(await self.hook({"fallbacks": ["claude-opus-5-5", {ROUTED: ["claude-opus-5-5"]}]}))
        self.assertIsNone(await self.hook({"tools": [{"type": "function", "function": {
            "name": "connect", "parameters": {"type": "object", "properties": {"api_key": {"type": "string"}}}}}]}))

    async def test_body_credentials_get_http_400(self) -> None:
        bearer = {"Authorization": "Bearer caller-key-placeholder"}
        for body in (
            {"api_key": "caller-key-placeholder"},
            {"aws_access_key_id": "caller-id-placeholder", "aws_secret_access_key": "caller-secret-placeholder"},
            {"aws_session_token": "caller-token-placeholder"},
            {"extra_body": {"api_key": "caller-key-placeholder"}},
            {"extra_headers": {"authorization": "Bearer caller-key-placeholder"}},
            # LiteLLM merges these two into the same outbound header dict as extra_headers.
            {"headers": bearer},
            {"provider_specific_header": {"custom_llm_provider": "bedrock", "extra_headers": bearer}},
            {"provider_specific_header": [{"custom_llm_provider": "bedrock", "extra_headers": bearer}]},
            # dict() accepts a list of [name, value] pairs as a header container too.
            {"extra_headers": [["Authorization", "Bearer caller-key-placeholder"]]},
            {"headers": [["authorization", "Bearer caller-key-placeholder"]]},
            # The router merges a fallback dict target into the call with kwargs.update().
            {"fallbacks": [{ROUTED: [{"model": ROUTED, "api_key": "caller-key-placeholder"}]}]},
            {"context_window_fallbacks": [{"model": ROUTED, "aws_access_key_id": "caller-id-placeholder"}]},
            {"content_policy_fallbacks": [{ROUTED: [{"model": ROUTED, "extra_headers": bearer}]}]},
        ):
            with self.assertRaises(HTTPException) as rejected:
                await self.hook(body)
            self.assertEqual(rejected.exception.status_code, 400, body)
            self.assertIn("Rejected Request", str(rejected.exception.detail), body)


class RandomProfile(ProfileRouter):
    profile = "random"

    async def test_selections_stay_in_the_pool_and_vary(self) -> None:
        pool = group(self.doc, ROUTED)
        picks = [await self.ask(ROUTED, [{"role": "user", "content": f"request {i}"}]) for i in range(30)]
        self.assertTrue(set(picks) <= set(pool), picks)
        self.assertGreater(len(set(picks)), 1, picks)

    async def test_switchyard_weights_decide_the_pick(self) -> None:
        pool = group(self.doc, ROUTED)
        weights = [0] * (len(pool) - 1) + [1]
        router = self.routed_by(MultiCandidateRouting(RandomRoutingPlugin(weights=weights)))
        last = pool[-1]
        picks = [await self.ask(ROUTED, [{"role": "user", "content": f"request {i}"}], router) for i in range(20)]
        self.assertEqual(set(picks), {last}, picks)

    async def test_baseline_groups_pass_through_to_their_one_model(self) -> None:
        for name in self.baselines():
            (model,) = group(self.doc, name)
            self.assertEqual(await self.ask(name, [{"role": "user", "content": "hi"}]), model)


if __name__ == "__main__":
    unittest.main()
