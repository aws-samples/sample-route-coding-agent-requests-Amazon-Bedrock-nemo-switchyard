#!/usr/bin/env bash
# Preflight checks before `cdk deploy`.
#
# Verifies the local toolchain, the AWS identity and Region, CDK bootstrap,
# the Docker build platform, and, for every model in profiles/<profile>/litellm.yaml,
# that the model (or the inference profile and its underlying model) exists
# in the target Region and that this account is authorized to invoke it.
#
# Read-only: every AWS call is a describe/get/list.
#
# Usage:
#   scripts/preflight.sh                       # Region from AWS_REGION / CLI config
#   AWS_REGION=us-east-1 scripts/preflight.sh
#   CPU_ARCHITECTURE=x86_64 scripts/preflight.sh   # match -c cpuArchitecture=...
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROUTING_PROFILE="${ROUTING_PROFILE:-stage}"
if [[ ! "$ROUTING_PROFILE" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "Invalid ROUTING_PROFILE: use a directory name under profiles/" >&2
  exit 2
fi
LITELLM_YAML="$ROOT/profiles/$ROUTING_PROFILE/litellm.yaml"
if ! (cd "$ROOT" && node --import tsx scripts/validate-profile.ts "$ROUTING_PROFILE"); then
  exit 2
fi
CPU_ARCHITECTURE="${CPU_ARCHITECTURE:-arm64}"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

FAILURES=0
WARNINGS=0
ok()   { green "  ok    $*"; }
warn() { yellow "  warn  $*"; WARNINGS=$((WARNINGS + 1)); }
fail() { red "  FAIL  $*"; FAILURES=$((FAILURES + 1)); }

version_ge() { # version_ge <have> <want>  (numeric dotted compare)
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# --- 1. toolchain --------------------------------------------------------------
bold "1. Toolchain"
if command -v node >/dev/null; then
  NODE_VERSION="$(node -v | sed 's/^v//')"
  if version_ge "$NODE_VERSION" 20.0.0; then ok "node $NODE_VERSION"; else fail "node $NODE_VERSION (need >= 20)"; fi
else
  fail "node not found (need >= 20)"
fi
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) (daemon running)"
else
  fail "docker not found or the daemon is not running (needed to build the gateway image)"
fi
if command -v aws >/dev/null; then ok "$(aws --version 2>&1 | cut -d' ' -f1)"; else fail "aws CLI not found"; fi
if [ -d "$ROOT/node_modules" ]; then
  ok "cdk $(cd "$ROOT" && npx cdk --version 2>/dev/null | cut -d' ' -f1) (from node_modules)"
else
  warn "node_modules missing: run 'npm ci' first"
fi
if command -v python3 >/dev/null; then
  ok "python3 $(python3 --version 2>&1 | cut -d' ' -f2) (smoke test and bench)"
else
  warn "python3 not found (needed by scripts/smoke-test.sh and bench/run.py)"
fi

# --- 2. identity and region ------------------------------------------------------
bold "2. AWS identity and Region"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [ -z "$REGION" ]; then
  fail "no Region configured. Export AWS_REGION (for example AWS_REGION=us-east-1)"
  REGION="us-east-1"
else
  ok "Region $REGION (CDK deploys to CDK_DEFAULT_REGION, which the CLI derives from the same setting)"
fi
if IDENTITY="$(aws sts get-caller-identity --output json 2>/dev/null)"; then
  ACCOUNT="$(printf '%s' "$IDENTITY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')"
  ARN="$(printf '%s' "$IDENTITY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')"
  ok "account ${ACCOUNT:0:4}******** as ${ARN#*:*:*:*:*:}"
  if printf '%s' "$ARN" | grep -qiE 'prod'; then
    warn "the caller ARN contains 'prod'. Deploy this sample to a non-production account."
  fi
else
  fail "aws sts get-caller-identity failed: configure credentials first"
  ACCOUNT=""
fi

# --- 3. CDK bootstrap -----------------------------------------------------------
bold "3. CDK bootstrap"
if [ -n "$ACCOUNT" ]; then
  if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" >/dev/null 2>&1; then
    ok "CDKToolkit stack present in $REGION"
  else
    warn "no CDKToolkit stack in $REGION. Run: npx cdk bootstrap aws://$ACCOUNT/$REGION"
  fi
fi

# --- 4. build platform ----------------------------------------------------------
bold "4. Container build platform"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64|aarch64) HOST_ARCH=arm64 ;;
  x86_64|amd64)  HOST_ARCH=x86_64 ;;
esac
if [ "$HOST_ARCH" = "$CPU_ARCHITECTURE" ]; then
  ok "host is $HOST_ARCH and cpuArchitecture=$CPU_ARCHITECTURE: native image build"
else
  warn "host is $HOST_ARCH but cpuArchitecture=$CPU_ARCHITECTURE: the Rust build runs under emulation and takes much longer. Consider -c cpuArchitecture=$HOST_ARCH"
fi

# --- 5. model availability -------------------------------------------------------
bold "5. Amazon Bedrock model availability in $REGION"
# Models are the `model: bedrock/...` lines of profiles/<profile>/litellm.yaml.
# POSIX character classes keep this portable to macOS (BSD) sed and grep.
MODELS="$(grep -E '^[[:space:]]*model:[[:space:]]*bedrock/' "$LITELLM_YAML" \
  | sed -E 's/^[[:space:]]*model:[[:space:]]*bedrock\///; s/^(converse|invoke|chat_completions)\///; s/[[:space:]]+#.*$//; s/[[:space:]]+$//' | sort -u)"
if [ -z "$MODELS" ]; then
  fail "no bedrock/ models found in $LITELLM_YAML"
fi

availability() { # availability <base model id>  -> prints "AUTHORIZED|<status>" or error text
  aws bedrock get-foundation-model-availability --region "$REGION" --model-id "$1" --output json 2>&1 \
    | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    print("ERROR " + raw.strip().splitlines()[-1][:120] if raw.strip() else "ERROR no response"); sys.exit(0)
print(" ".join([d.get("authorizationStatus", "?"), d.get("agreementAvailability", {}).get("status", "?"), d.get("entitlementAvailability", "?"), d.get("regionAvailability", "?")]))'
}

printf '  %-48s %-10s %-11s %-10s %-10s %s\n' MODEL AUTH AGREEMENT ENTITLE REGION NOTE
while IFS= read -r model; do
  [ -z "$model" ] && continue
  first="${model%%.*}"
  note=""
  base="$model"
  case "$first" in
    global|us|eu|apac|us-gov|jp|au)
      # Resolve the inference profile to its underlying foundation model.
      if PROFILE="$(aws bedrock get-inference-profile --region "$REGION" --inference-profile-identifier "$model" --output json 2>/dev/null)"; then
        base="$(printf '%s' "$PROFILE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["models"][0]["modelArn"].split("/")[-1])')"
        status="$(printf '%s' "$PROFILE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
        note="profile $status -> $base"
      else
        printf '  %-48s ' "$model"; fail "inference profile not found in $REGION"
        continue
      fi
      ;;
  esac
  result="$(availability "$base")"
  case "$result" in
    AUTHORIZED*)
      read -r auth agreement entitlement regional <<<"$result"
      if [ "$agreement" = "AVAILABLE" ] && [ "$entitlement" = "AVAILABLE" ] && [ "$regional" = "AVAILABLE" ]; then
        printf '  %-48s %-10s %-11s %-10s %-10s %s\n' "$model" "$auth" "$agreement" "$entitlement" "$regional" "$note"
      else
        printf '  %-48s %-10s %-11s %-10s %-10s %s\n' "$model" "$auth" "$agreement" "$entitlement" "$regional" "$note"
        fail "$model: enable model access in the Amazon Bedrock console (Model access) for $REGION"
      fi
      ;;
    NOT_AUTHORIZED*|UNAUTHORIZED*|*)
      printf '  %-48s %s %s\n' "$model" "$result" "$note"
      fail "$model: not authorized or unavailable. Request access in the Amazon Bedrock console, or replace the model in profiles/<profile>/litellm.yaml"
      ;;
  esac
done <<<"$MODELS"

# --- summary --------------------------------------------------------------------
echo
if [ "$FAILURES" -eq 0 ]; then
  if [ "$WARNINGS" -eq 0 ]; then green "Preflight passed."; else yellow "Preflight passed with $WARNINGS warning(s)."; fi
  echo "Next: npx cdk deploy -c routingProfile=$ROUTING_PROFILE -c allowedCidr=\"\$(curl -s https://checkip.amazonaws.com)/32\" -c domainName=<host> -c hostedZone=<zone>"
  echo "      (or -c certificateArn=<arn> in place of hostedZone; for a short plain-HTTP test, -c allowPlainHttp=true in place of both HTTPS settings)"
else
  red "Preflight found $FAILURES problem(s). Fix them before deploying."
  exit 1
fi
