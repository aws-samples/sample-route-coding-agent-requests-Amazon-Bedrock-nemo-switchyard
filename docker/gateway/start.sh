#!/bin/sh
set -eu
umask 077

# ECS injects the generated suffix. Compose supplies a complete local key.
if [ -n "${LITELLM_MASTER_KEY_SUFFIX:-}" ]; then
  export LITELLM_MASTER_KEY="sk-$LITELLM_MASTER_KEY_SUFFIX"
  unset LITELLM_MASTER_KEY_SUFFIX
fi
: "${LITELLM_MASTER_KEY:?A LiteLLM master key is required}"

if [ -n "${LITELLM_CONFIG_TEXT:-}" ]; then
  printf '%s' "$LITELLM_CONFIG_TEXT" > /tmp/litellm.yaml
  export LITELLM_CONFIG=/tmp/litellm.yaml
fi
if [ -n "${SWITCHYARD_CONFIG_TEXT:-}" ]; then
  printf '%s' "$SWITCHYARD_CONFIG_TEXT" > /tmp/switchyard.toml
  export SWITCHYARD_LITELLM_CONFIG=/tmp/switchyard.toml
fi
: "${LITELLM_CONFIG:?Set LITELLM_CONFIG or LITELLM_CONFIG_TEXT to the LiteLLM YAML}"
: "${SWITCHYARD_LITELLM_CONFIG:?Set SWITCHYARD_LITELLM_CONFIG or SWITCHYARD_CONFIG_TEXT to the Switchyard TOML}"
if [ ! -r "$LITELLM_CONFIG" ]; then
  echo "LiteLLM config $LITELLM_CONFIG is missing or not readable by uid $(id -u)" >&2
  exit 1
fi
if [ ! -r "$SWITCHYARD_LITELLM_CONFIG" ]; then
  echo "Switchyard config $SWITCHYARD_LITELLM_CONFIG is missing or not readable by uid $(id -u)" >&2
  exit 1
fi

# The image exposes this port; the ECS target group and Compose map to it.
# Binding all interfaces inside the container is what lets them reach it;
# Compose publishes it on 127.0.0.1 only.
port=4000
exec litellm --config "$LITELLM_CONFIG" --host 0.0.0.0 --port "$port" "$@"
