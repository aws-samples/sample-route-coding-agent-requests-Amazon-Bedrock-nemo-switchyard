"""Route multi-deployment model groups with Switchyard; pass single-model groups through.

LiteLLM runs routing plugins for every model group. Switchyard's Stage plugin
requires exactly two candidates and fails closed otherwise, so a baseline group
with one deployment (for example `claude-opus-5-5`) would reject every request.
This wrapper leaves groups with a single unique candidate untouched and hands
every other group to the configured Switchyard plugin.

The callback half (`async_pre_call_deployment_hook`, inherited) applies any
request patch Switchyard recorded in the request metadata after LiteLLM selects
the deployment. It reads the patch from metadata, not from instance state, so
it works unchanged for the wrapped plugin's decisions.

The same callback registration gives the proxy's pre-call hook, which runs on
every request before routing. The proxy calls `async_pre_call_hook` only when
the registered object's own class defines it, so it is defined here and not in
a base class. It rejects a request that carries its own credential anywhere in
its body (`api_key`, AWS keys, or an `Authorization` header in `extra_headers`,
`headers` or `provider_specific_header`; see `request_guard`): LiteLLM would
otherwise keep a body `api_key` as an extra deployment of the model group and
reuse it for other callers' requests, merge a fallback target's credentials into
the call, and send a caller's `Authorization` header in place of the task role's
signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from litellm.types.router import RoutingContext
from switchyard_litellm.configuration.loader import RoutingPlugin
from switchyard_litellm.plugins.lite_llm_request_rewriter import LiteLLMRequestRewriter

from .request_guard import REJECTION, credential_field

if TYPE_CHECKING:
    from litellm.caching.caching import DualCache
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.types.utils import CallTypesLiteral


class MultiCandidateRouting(LiteLLMRequestRewriter):
    """Apply a Switchyard routing plugin only where there is a choice to make."""

    def __init__(self, inner: RoutingPlugin) -> None:
        super().__init__()
        self._inner = inner

    async def run(self, context: RoutingContext) -> RoutingContext:
        if len(set(context.candidate_models)) < 2:
            return context
        return await self._inner.run(context)

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> dict | None:
        """Reject a request that carries a caller credential; pass every other request through."""
        del user_api_key_dict, cache, call_type
        field = credential_field(data)
        if field is not None:
            raise HTTPException(status_code=400, detail=REJECTION.format(field=field))
        return None
