#!/usr/bin/env bash
# Validates the selected plugin profile against a running gateway.
# Reads the URL and key from the stack outputs unless GATEWAY_URL is set;
# LITELLM_MASTER_KEY is used only together with GATEWAY_URL.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/smoke-test.py" "$@"
