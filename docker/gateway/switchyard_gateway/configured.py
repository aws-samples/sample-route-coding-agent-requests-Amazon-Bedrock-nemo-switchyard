"""The routing plugin object LiteLLM imports by dotted path at proxy start.

Reference it from litellm.yaml in both places:
  router_settings.plugins:     switchyard_gateway.configured.ROUTING_PLUGIN
  litellm_settings.callbacks:  switchyard_gateway.configured.ROUTING_PLUGIN

SWITCHYARD_LITELLM_CONFIG names the profile's switchyard.toml. Invalid TOML
fails here, at proxy start, before any request is served.
"""

from switchyard_litellm.configuration.loader import load_routing_plugin_from_environment

from .plugin import MultiCandidateRouting

ROUTING_PLUGIN = MultiCandidateRouting(load_routing_plugin_from_environment())
