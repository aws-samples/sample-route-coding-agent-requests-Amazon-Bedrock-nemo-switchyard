#!/usr/bin/env python3
"""Check authentication, model groups, and plugin selection. Makes provider calls."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
from run import canonical_model, open_url  # noqa: E402

# Values from the environment and the stack outputs become single arguments to
# the AWS CLI and node (never a shell), and must also match their own format.
FORMATS = {
    "AWS Region": (re.compile(r"[a-z]{2}(-[a-z]+)+-\d+", re.ASCII), "us-east-1"),
    "STACK_NAME": (re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}", re.ASCII), "SwitchyardGateway"),
    "ROUTING_PROFILE": (re.compile(r"[a-z][a-z0-9-]*", re.ASCII), "stage"),
    "MasterKeySecretArn": (
        re.compile(r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+", re.ASCII),
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:name"),
}


def validated(setting, value):
    """value, when all of it matches the format FORMATS gives for setting; ValueError otherwise."""
    pattern, example = FORMATS[setting]
    if not pattern.fullmatch(value):
        raise ValueError(f"{setting} {value!r} is not valid; expected a value like {example}")
    return value


def executable(name):
    """Absolute path of a command on PATH, so no subprocess starts from a partial path."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is not on PATH")
    return path


def aws(*arguments):
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    command = [executable("aws"), *arguments, "--output", "json"]
    if region:
        command += ["--region", validated("AWS Region", region)]
    # argv list with no shell; the callers validate the stack name and secret ARN.
    return json.loads(subprocess.check_output(command, text=True))  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args


# LiteLLM routes the load balancer must answer itself: admin UI, API docs, key
# management, and the health routes (/health calls every model when given a key).
CLOSED_PATHS = ["/ui", "/redoc", "/openapi.json", "/key/list", "/sso/key/generate",
                "/health", "/health/readiness"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
LOAD_BALANCER_NOT_FOUND = "Not found"


def is_local_gateway(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower() in LOCAL_HOSTS


def fetch_status(url, path):
    """Status and body text without a key, so a misrouted /health never calls models."""
    try:
        with open_url(url.rstrip("/") + path, timeout=30) as response:
            return response.status, response.read(4096).decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096).decode(errors="replace")
    except OSError as error:  # URLError, timeouts, and connection resets
        reason = getattr(error, "reason", error)
        raise AssertionError(f"{path}: gateway unreachable ({reason})") from error


def check_closed_paths(url, fetch=fetch_status):
    if is_local_gateway(url):
        print("Closed paths: skipped for a local gateway (no load balancer)")
        return
    for path in CLOSED_PATHS:
        status, text = fetch(url, path)
        assert status == 404, f"{path}: expected HTTP 404 from the load balancer, got HTTP {status}"
        assert text.strip() == LOAD_BALANCER_NOT_FOUND, (
            f"{path}: HTTP 404 did not come from the load balancer default action")
    print("Closed paths:", ", ".join(CLOSED_PATHS))


def check_baselines(manifest, select):
    """Send one request to each baseline group; each must answer with its own model."""
    for group, model in manifest["baselines"].items():
        answered = canonical_model(select(group))
        assert answered == canonical_model(model), (
            f"Baseline {group}: expected {canonical_model(model)}, got {answered}")


def resolve_gateway(environ, from_stack):
    """URL, key, and a description of where they came from.

    An exported key is trusted only together with GATEWAY_URL, so a key left in
    the shell from a local run is never sent to the stack's gateway.
    """
    url, key = environ.get("GATEWAY_URL"), environ.get("LITELLM_MASTER_KEY")
    stack_name = environ.get("STACK_NAME", "SwitchyardGateway")
    if url and key:
        return url, key, "GATEWAY_URL and LITELLM_MASTER_KEY from the environment"
    stack_url, stack_key = from_stack(stack_name)
    if url:
        return url, stack_key, f"GATEWAY_URL from the environment, key from stack {stack_name}"
    source = f"URL and key from stack {stack_name}"
    if key:
        source += " (exported LITELLM_MASTER_KEY ignored because GATEWAY_URL is unset)"
    return stack_url, stack_key, source


def request(url, path, key=None, body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with open_url(req, timeout=180) as response:
            return response.status, response.headers, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, {}


def main():
    profile = validated("ROUTING_PROFILE", os.environ.get("ROUTING_PROFILE", "stage"))
    # argv list with no shell; the profile name is validated above.
    manifest = json.loads(subprocess.check_output([  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
        executable("node"), "--import", "tsx",
        str(ROOT / "scripts" / "validate-profile.ts"), profile], text=True, cwd=ROOT))

    def from_stack(stack_name):
        stack = aws("cloudformation", "describe-stacks",
                    "--stack-name", validated("STACK_NAME", stack_name))["Stacks"][0]
        outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in stack["Outputs"]}
        deployed = outputs.get("RoutingProfile")
        if deployed and deployed != profile:
            raise RuntimeError(f"Stack uses {deployed}; set ROUTING_PROFILE={deployed}")
        return outputs["GatewayUrl"], "sk-" + aws(
            "secretsmanager", "get-secret-value",
            "--secret-id", validated("MasterKeySecretArn", outputs["MasterKeySecretArn"]))["SecretString"]

    url, key, source = resolve_gateway(os.environ, from_stack)
    print(f"Gateway: {url}\nSource: {source}\nProfile: {profile}\n"
          f"Model group: {manifest['modelGroup']}\n"
          f"Baseline groups: {', '.join(manifest['baselines'])}")
    status, _, _ = request(url, "/health/liveliness")
    assert status == 200, f"Health: HTTP {status}"
    check_closed_paths(url)
    body = {"model": manifest["modelGroup"], "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with the single word pong."}]}
    status, _, _ = request(url, "/v1/chat/completions", body=body)
    assert status == 401, f"Expected missing-key rejection, got HTTP {status}"
    status, _, listing = request(url, "/v1/models", key)
    listed = {entry["id"] for entry in listing.get("data", [])}
    assert status == 200, f"Model list: HTTP {status}"
    for group in [manifest["modelGroup"], *manifest["baselines"]]:
        assert group in listed, f"Model group {group} missing from /v1/models"

    def selected(payload):
        status, headers, _ = request(url, "/v1/chat/completions", key, payload)
        assert status == 200, f"Inference: HTTP {status}"
        model = headers.get("x-litellm-model-name")
        assert model, "Missing x-litellm-model-name"
        print(f"{payload['model']}: {model}")
        return canonical_model(model)

    candidates = [canonical_model(model) for model in manifest["models"]]
    if manifest["algorithm"] == "stage":
        assert selected(body) == candidates[1], "Expected the efficient candidate"
        fixture = json.loads((ROOT / "scripts" / "fixtures" / "stage-escalation.json").read_text())
        fixture["model"] = manifest["modelGroup"]
        assert selected(fixture) == candidates[0], "Expected escalation to the capable candidate"
    else:
        for _ in range(4):
            assert selected(body) in candidates, "Random selected outside the configured pool"
    check_baselines(manifest, lambda group: selected({**body, "model": group}))
    print("All checks passed.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print("FAIL:", error, file=sys.stderr)
        sys.exit(1)
