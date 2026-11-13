#!/usr/bin/env python3
"""Offline proxy tests against a local mock provider. No AWS credentials/calls."""
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
import run as demo

PLUGIN = "switchyard_gateway.configured.ROUTING_PLUGIN"
ROUTED = ["openai/capable", "openai/efficient"]
# One-model baseline groups, named like the shipped profiles' aliases.
BASELINES = {"capable": "openai/capable", "efficient": "openai/efficient"}
# The proxy container reads its config from a bind mount. Colima and Docker Desktop
# share only parts of the host filesystem (not /tmp or /var/folders), so the default
# is a gitignored directory inside the repository. E2E_TMPDIR overrides it.
TEMP_ROOT = Path(os.environ.get("E2E_TMPDIR") or ROOT / ".e2e-tmp")


LOOPBACK = "127.0.0.1"
HOST_HINT = "Set E2E_PROVIDER_HOST to a host address the proxy container can reach."


def choose_provider_host(system, override, docker_info, bridge_networks):
    """Address for the mock provider that the proxy container can reach.

    Docker Desktop and Colima/Lima forward host.docker.internal to the host's
    loopback, so the mock binds 127.0.0.1. Docker Engine on Linux maps
    host-gateway to the default bridge gateway, which a loopback socket does not
    answer, so the mock binds that bridge address. It is not reachable through
    normal routing from other machines; CI runners are ephemeral. Rootless Docker
    has no address that works everywhere (rootlesskit defaults to
    --disable-host-loopback), so it requires E2E_PROVIDER_HOST.

    docker_info is `docker info` as JSON; bridge_networks is
    `docker network inspect bridge` output. Both are unused when not needed.
    """
    if override:
        try:
            return str(ipaddress.ip_address(override))
        except ValueError:
            raise RuntimeError(f"E2E_PROVIDER_HOST={override!r} is not an IP address. "
                               + HOST_HINT) from None
    if system == "Darwin" or forwards_to_host_loopback(docker_info):
        return LOOPBACK
    if is_rootless(docker_info):
        raise RuntimeError("Rootless Docker does not forward host.docker.internal to the "
                           "host's loopback by default. " + HOST_HINT)
    for network in bridge_networks or []:
        for config in (network.get("IPAM") or {}).get("Config") or []:
            try:
                return str(ipaddress.IPv4Address(config.get("Gateway", "")))
            except ValueError:
                continue
    raise RuntimeError("The Docker bridge network has no IPv4 gateway. " + HOST_HINT)


def forwards_to_host_loopback(docker_info):
    info = docker_info or {}
    name = str(info.get("Name", "")).lower()
    return ("Docker Desktop" in str(info.get("OperatingSystem", ""))
            or name == "colima" or name.startswith(("colima-", "lima-")))


def is_rootless(docker_info):
    options = (docker_info or {}).get("SecurityOptions") or []
    return any("rootless" in str(option) for option in options)


DEFAULT_READY_TIMEOUT = 180.0


def ready_timeout(environ):
    """Seconds to wait for the proxy to report healthy (E2E_READY_TIMEOUT).

    LiteLLM can take more than a minute to start on a loaded host.
    """
    raw = environ.get("E2E_READY_TIMEOUT") or ""
    if not raw:
        return DEFAULT_READY_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not (0 < value < float("inf")):
        raise RuntimeError(f"E2E_READY_TIMEOUT={raw!r} must be a positive number of seconds.")
    return value


DEFAULT_IMAGE = "switchyard-litellm:local"
# [registry[:port]/]name[:tag][@sha256:digest]. It starts with a letter or digit
# and has no whitespace or shell characters, so it stays one image argument.
IMAGE_REFERENCE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*"
    r"(?::\w[\w.-]{0,127})?(?:@sha256:[0-9a-f]{64})?", re.ASCII)


def gateway_image(environ):
    """GATEWAY_IMAGE (default switchyard-litellm:local), checked to be a Docker image reference."""
    image = environ.get("GATEWAY_IMAGE", DEFAULT_IMAGE)
    if not IMAGE_REFERENCE.fullmatch(image):
        raise RuntimeError(f"GATEWAY_IMAGE={image!r} is not a Docker image reference "
                           f"such as {DEFAULT_IMAGE}.")
    return image


def docker_json(*arguments):
    try:
        # The Docker CLI from PATH with a fixed subcommand list and no shell.
        result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True)  # nosec B603, B607
        return json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        detail = getattr(error, "stderr", "") or error
        raise RuntimeError(f"`docker {' '.join(arguments)}` failed: {detail}. " + HOST_HINT) from error


def provider_bind_address():
    override = os.environ.get("E2E_PROVIDER_HOST")
    system = platform.system()
    if override or system == "Darwin":
        return choose_provider_host(system, override, None, None)
    info = docker_json("info", "--format", "{{json .}}")
    needs_bridge = not (forwards_to_host_loopback(info) or is_rootless(info))
    bridge = docker_json("network", "inspect", "bridge") if needs_bridge else None
    return choose_provider_host(system, None, info, bridge)


def proxy_run_command(name, config_directory, image, provider_host, system):
    """`docker run` argv for the proxy, publishing its port on the host's loopback only.

    The proxy reaches the mock provider at host.docker.internal. On macOS the
    engine resolves that name to the host's loopback. Elsewhere a loopback mock
    needs host-gateway, and any other address (the Linux bridge gateway or
    E2E_PROVIDER_HOST) is mapped explicitly so the name points where the mock listens.
    """
    command = ["docker", "run", "-d", "--rm", "--name", name,
               "-p", "127.0.0.1::4000", "-v", f"{config_directory}:/config:ro",
               "-e", "LITELLM_MASTER_KEY=sk-offline-test-key",
               "-e", "LITELLM_CONFIG=/config/litellm.yaml",
               "-e", "SWITCHYARD_LITELLM_CONFIG=/config/switchyard.toml"]
    if provider_host != LOOPBACK:
        command += ["--add-host", f"host.docker.internal:{provider_host}"]
    elif system != "Darwin":
        command += ["--add-host", "host.docker.internal:host-gateway"]
    return command + [image]


SLUG = """import re

def slugify(text):
    text = re.sub(r"[^a-z0-9\\s-]", "", text.lower())
    return "-".join(text.split())
"""


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = request["messages"]
        model = request["model"]
        message = {"role": "assistant", "content": "pong"}
        if request.get("tools") and any("slug.py" in str(item.get("content", "")) for item in messages):
            tools = [item.get("name") for item in messages if item["role"] == "tool"]
            # Track tool names through the preceding assistant calls.
            tools = [call["function"]["name"] for item in messages
                     for call in item.get("tool_calls", [])]
            if "run_tests" in tools:
                message["content"] = "Fixed slug.py."
            else:
                if "write_file" in tools:
                    name, arguments = "run_tests", {}
                elif "read_file" in tools:
                    name, arguments = "write_file", {"path": "slug.py", "content": SLUG}
                else:
                    name, arguments = "read_file", {"path": "slug.py"}
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        payload = {"id": "chatcmpl-offline", "object": "chat.completion", "created": int(time.time()),
                   "model": model, "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def chat(url, key="sk-offline-test-key", body=None):
    payload = body or {"model": "switchyard", "max_tokens": 64,
                      "messages": [{"role": "user", "content": "Reply pong."}]}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url + "/v1/chat/completions", json.dumps(payload).encode(), headers=headers)
    return demo.open_url(request, timeout=30)


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host = provider_bind_address()
        try:
            cls.provider = ThreadingHTTPServer((host, 0), Provider)
        except OSError as error:
            raise RuntimeError(f"Could not bind the mock provider to {host}: {error}. "
                               + HOST_HINT) from error
        cls.thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()

    def start_proxy(self, algorithm):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="switchyard-proxy-test-", dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        directory.chmod(0o755)
        endpoint = f"http://host.docker.internal:{self.provider.server_port}/v1"
        groups = [("switchyard", model) for model in ROUTED] + list(BASELINES.items())
        yaml = "model_list:\n"
        for group, model in groups:
            yaml += f"""  - model_name: {group}
    litellm_params:
      model: {model}
      api_base: {endpoint}
      api_key: offline-provider-key
"""
        yaml += f"""router_settings:
  plugins:
    - {PLUGIN}
litellm_settings:
  callbacks:
    - {PLUGIN}
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
        (directory / "litellm.yaml").write_text(yaml)
        (directory / "switchyard.toml").write_text((ROOT / "profiles" / algorithm / "switchyard.toml").read_text())
        name = "switchyard-offline-" + uuid.uuid4().hex[:8]
        command = proxy_run_command(name, directory, gateway_image(os.environ),
                                    self.provider.server_address[0], platform.system())
        # Docker CLI argv lists with no shell: the image is validated and the name is generated above.
        subprocess.run(command, check=True, capture_output=True, text=True)  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
        self.addCleanup(lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True))  # nosec B603, B607
        inspected = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]  # nosec B603, B607
        port = inspected["NetworkSettings"]["Ports"]["4000/tcp"][0]["HostPort"]
        url = "http://127.0.0.1:" + port
        timeout = ready_timeout(os.environ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with demo.open_url(url + "/health/liveliness", timeout=1) as response:
                    if response.status == 200:
                        return url
            except (OSError, urllib.error.URLError):
                time.sleep(0.5)
        # Docker CLI argv list with no shell, for the container this test started.
        logs = subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT, text=True)  # nosec B603, B607
        self.fail(f"Proxy did not become healthy within {timeout:g} s "
                  "(set E2E_READY_TIMEOUT to wait longer):\n" + logs[-5000:])

    def test_stage_auth_selection_escalation_and_coding_artifacts(self):
        url = self.start_proxy("stage")
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            chat(url, key=None)
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()
        # The gateway refuses a caller credential in the body; LiteLLM would keep a body api_key.
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            chat(url, body={"model": "switchyard", "max_tokens": 64, "api_key": "caller-key-placeholder",
                            "messages": [{"role": "user", "content": "Reply pong."}]})
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn("Rejected Request: api_key", rejected.exception.read().decode())
        rejected.exception.close()
        # A body `headers` entry would replace the task role's Authorization header on the call.
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            chat(url, body={"model": "switchyard", "max_tokens": 64,
                            "headers": {"Authorization": "Bearer caller-key-placeholder"},
                            "messages": [{"role": "user", "content": "Reply pong."}]})
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn("Rejected Request: headers.Authorization", rejected.exception.read().decode())
        rejected.exception.close()
        # The router would merge a per-request fallback dict target, api_key included, into the call.
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            chat(url, body={"model": "switchyard", "max_tokens": 64,
                            "fallbacks": [{"switchyard": [{"model": "switchyard", "api_key": "caller-key-placeholder"}]}],
                            "messages": [{"role": "user", "content": "Reply pong."}]})
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn("Rejected Request: fallbacks[0].switchyard[0].api_key", rejected.exception.read().decode())
        rejected.exception.close()
        with chat(url) as response:
            self.assertEqual(response.headers["x-litellm-model-name"], "openai/efficient")
            self.assertEqual(json.load(response)["model"], "switchyard")
        fixture = json.loads((ROOT / "scripts/fixtures/stage-escalation.json").read_text())
        with chat(url, body=fixture) as response:
            self.assertEqual(response.headers["x-litellm-model-name"], "openai/capable")
        with tempfile.TemporaryDirectory() as directory:
            result = demo.solve(demo.Gateway(url, "sk-offline-test-key"), "switchyard",
                                "slugify", Path(directory), max_turns=5)
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["turns"], 3, "Stop model calls after the supplied tests pass")
            artifact = Path(result["artifacts"])
            self.assertIn("slug.py", (artifact / "changes.patch").read_text())
            self.assertTrue((artifact / "transcript.jsonl").is_file())
            self.assertTrue((artifact / "workspace/slug.py").is_file())
            self.assertIsNone(result["estimated_cost"])

    def assert_baselines_answer_with_their_model(self, url):
        for group, model in BASELINES.items():
            body = {"model": group, "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Reply pong."}]}
            for _ in range(3):
                with chat(url, body=body) as response:
                    self.assertEqual(response.headers["x-litellm-model-name"], model, group)
                    self.assertEqual(json.load(response)["model"], group)
        # The fixture would escalate on the routed group; a baseline ignores it.
        fixture = json.loads((ROOT / "scripts/fixtures/stage-escalation.json").read_text())
        with chat(url, body={**fixture, "model": "efficient"}) as response:
            self.assertEqual(response.headers["x-litellm-model-name"], "openai/efficient")

    def test_stage_baseline_groups_pass_through(self):
        self.assert_baselines_answer_with_their_model(self.start_proxy("stage"))

    def test_random_stays_inside_candidate_pool(self):
        url = self.start_proxy("random")
        for _ in range(6):
            with chat(url) as response:
                self.assertIn(response.headers["x-litellm-model-name"], ROUTED)
        self.assert_baselines_answer_with_their_model(url)

    def test_models_lists_routed_and_baseline_groups(self):
        url = self.start_proxy("stage")
        request = urllib.request.Request(
            url + "/v1/models", headers={"Authorization": "Bearer sk-offline-test-key"})
        with demo.open_url(request, timeout=30) as response:
            ids = sorted(entry["id"] for entry in json.load(response)["data"])
        self.assertEqual(ids, sorted(["switchyard", *BASELINES]))

    def test_missing_usage_is_unpriced(self):
        self.assertIsNone(demo.usage_cost("nvidia.nemotron-super-3-120b", {}))
        self.assertIsNone(demo.usage_cost("unknown/model", {"prompt_tokens": 10, "completion_tokens": 5}))
        self.assertIsNone(demo.usage_cost("global.anthropic.claude-opus-5-5", {
            "prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 5}}))


if __name__ == "__main__":
    unittest.main()
