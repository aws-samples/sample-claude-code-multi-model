#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# mantle-token.sh — Generate a Bedrock Mantle bearer token from IAM credentials
#
# Uses aws-bedrock-token-generator to create a 12-hour bearer token
# for the Bedrock Mantle Chat Completions API.
#
# Usage:
#   eval $(./scripts/mantle-token.sh)           # export MANTLE_API_KEY
#   ./scripts/mantle-token.sh --print           # just print the token
#   ./scripts/mantle-token.sh --region us-west-2  # non-default region
# ---------------------------------------------------------------------------

REGION="${AWS_REGION:-us-east-1}"
ACTION="export"

while [[ $# -gt 0 ]]; do
    case $1 in
        --region|-r) REGION="$2"; shift 2 ;;
        --print|-p)  ACTION="print"; shift ;;
        -h|--help)
            echo "Usage: $0 [--region REGION] [--print]"
            echo ""
            echo "Generates a Bedrock Mantle bearer token from your AWS credentials."
            echo "Default region: us-east-1"
            echo ""
            echo "Options:"
            echo "  --region REGION   AWS region for token scope (default: us-east-1)"
            echo "  --print           Print token only (don't export)"
            exit 0
            ;;
        *) echo "[error] Unknown option: $1" >&2; exit 1 ;;
    esac
done

if ! python3 -c "import aws_bedrock_token_generator" 2>/dev/null; then
    echo "[install] Installing aws-bedrock-token-generator..." >&2
    python3 -m pip install aws-bedrock-token-generator --quiet >&2
fi

TOKEN=$(AWS_REGION="$REGION" python3 -c "
import os
os.environ['AWS_REGION'] = '${REGION}'
from aws_bedrock_token_generator import provide_token
print(provide_token(region='${REGION}'))
")

if [[ -z "$TOKEN" ]]; then
    echo "[error] Failed to generate Mantle token. Check AWS credentials." >&2
    exit 1
fi

case $ACTION in
    export)
        echo "export MANTLE_API_KEY='${TOKEN}'"
        echo "[token] Mantle bearer token generated for ${REGION} (valid 12h)" >&2
        ;;
    print)
        echo "$TOKEN"
        ;;
esac
