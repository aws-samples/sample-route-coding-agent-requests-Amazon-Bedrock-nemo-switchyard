"""Find a caller credential anywhere in a request body before LiteLLM can use it.

LiteLLM 1.102.2 reads credentials from more than one place in a
`/v1/chat/completions` body: `api_key`, `aws_access_key_id`,
`aws_secret_access_key` and `aws_session_token` at the top level or inside
`extra_body`; the same keys in a per-request fallback target (`fallbacks`,
`context_window_fallbacks`, `content_policy_fallbacks`, also under
`router_settings_override`), which the router merges into the call with
`kwargs.update()`; and an `Authorization` entry in any header container it
merges into the outbound request (`extra_headers`, `headers`, and each
`provider_specific_header` entry's `extra_headers`), given as a mapping or as a
list of `[name, value]` pairs, which `dict()` accepts too. A non-SigV4
`Authorization` header replaces the task role's signature on the Bedrock call.
A body `api_key` is also kept as a new deployment of the model group and reused
for later requests, including other callers'. The gateway signs every Bedrock
call with its task role, so this module walks the whole body (every mapping and
list, any depth) instead of listing the positions LiteLLM reads, and reports
the first credential it finds. It skips the content subtrees `messages`,
`tools`, `functions`, `tool_choice` and `response_format`, so a tool schema
with an `api_key` parameter still works. This module has no third-party
imports; the host-side unit tests load it on its own.
"""

from __future__ import annotations

from collections.abc import Mapping

CREDENTIAL_FIELDS = frozenset(("api_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"))
# LiteLLM merges all three into the header dict of the outbound request.
HEADER_CONTAINERS = frozenset(("extra_headers", "headers", "provider_specific_header"))
# Content for the model, never a credential; a tool schema may describe an `api_key` parameter.
CONTENT_FIELDS = frozenset(("messages", "tools", "functions", "tool_choice", "response_format"))
AUTHORIZATION = "authorization"
# docs/TROUBLESHOOTING.md quotes this message.
REJECTION = "Rejected Request: {field} is not accepted; the gateway signs every call with its task role"


def credential_field(data: Mapping[str, object]) -> str | None:
    """Return the path of the first request field that carries a caller credential, or None."""
    # An explicit stack: the body's depth is bounded by the JSON parser, not by this walk.
    stack: list[tuple[object, str, bool]] = [(data, "", False)]
    while stack:
        value, path, in_headers = stack.pop()
        if isinstance(value, Mapping):
            children = []
            for key, child in value.items():
                name = str(key)
                if name in CREDENTIAL_FIELDS:
                    return _join(path, name)
                if in_headers and name.lower() == AUTHORIZATION:
                    return _join(path, "Authorization")
                if name in CONTENT_FIELDS and not in_headers:
                    continue
                children.append((child, _join(path, name), in_headers or name in HEADER_CONTAINERS))
            stack.extend(reversed(children))
        elif isinstance(value, (list, tuple)):
            children = []
            for index, child in enumerate(value):
                # dict() turns a list of [name, value] pairs, or of two-key mappings, into a header dict.
                if in_headers and isinstance(child, str) and child.lower() == AUTHORIZATION:
                    return _join(path, "Authorization")
                children.append((child, f"{path}[{index}]", in_headers))
            stack.extend(reversed(children))
    return None


def _join(path: str, name: str) -> str:
    return name if not path else path + "." + name
