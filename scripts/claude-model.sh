#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# claude-model.sh — Run Claude Code with any Bedrock model
#
# For Anthropic models: connects directly to Bedrock (no proxy needed)
# For third-party models: routes through LiteLLM proxy -> Bedrock Mantle
#
# All 38 Mantle models support tools + streaming natively.
#
# Usage:
#   ./scripts/claude-model.sh                      # interactive: pick a model
#   ./scripts/claude-model.sh --model qwen-coder-next
#   ./scripts/claude-model.sh --model claude-opus   # native Bedrock
#   ./scripts/claude-model.sh --model claude-sonnet -p "explain this code"
#   ./scripts/claude-model.sh --list                # list available models
#
# Environment:
#   PROXY_PORT       LiteLLM proxy port (default: 4000)
#   AWS_REGION       AWS region for Bedrock (default: us-east-1)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROXY_PORT="${PROXY_PORT:-4000}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# ── Model Registry ────────────────────────────────────────────────
# Format: alias|type|model_id|description
# type: "native" = direct Bedrock, "proxy" = via LiteLLM -> Mantle
MODELS=(
    # ── Anthropic (native — no proxy needed) ──────────────────────
    "claude-opus|native|us.anthropic.claude-opus-4-6-v1|Claude Opus 4.6 — flagship, best reasoning"
    "claude-sonnet|native|us.anthropic.claude-sonnet-4-6|Claude Sonnet 4.6 — balanced speed/quality"
    "claude-haiku|native|us.anthropic.claude-haiku-4-5-20251001-v1:0|Claude Haiku 4.5 — fast, lightweight"
    "claude-opus-4.5|native|us.anthropic.claude-opus-4-5-20251101-v1:0|Claude Opus 4.5 — previous gen flagship"
    "claude-sonnet-4.5|native|us.anthropic.claude-sonnet-4-5-20250929-v1:0|Claude Sonnet 4.5 — previous gen balanced"

    # ── Qwen — Coding (via Mantle) ────────────────────────────────
    "qwen-coder-next|proxy|qwen-coder-next|Qwen3 Coder Next — latest coding model"
    "qwen-coder-480b|proxy|qwen-coder-480b|Qwen3 Coder 480B — largest coding MoE"
    "qwen-coder-30b|proxy|qwen-coder-30b|Qwen3 Coder 30B — compact coding MoE"

    # ── Qwen — General / Vision (via Mantle) ──────────────────────
    "qwen-235b|proxy|qwen-235b|Qwen3 235B — general purpose MoE"
    "qwen-32b|proxy|qwen-32b|Qwen3 32B — dense, hybrid thinking"
    "qwen-vl-235b|proxy|qwen-vl-235b|Qwen3 VL 235B — vision + language"
    "qwen-next-80b|proxy|qwen-next-80b|Qwen3 Next 80B — efficient MoE"

    # ── DeepSeek (via Mantle) ─────────────────────────────────────
    "deepseek-v3|proxy|deepseek-v3|DeepSeek V3.2 — coding + reasoning MoE"
    "deepseek-v3.1|proxy|deepseek-v3.1|DeepSeek V3.1 — previous gen"

    # ── Mistral AI (via Mantle) ───────────────────────────────────
    "devstral-123b|proxy|devstral-123b|Devstral 2 123B — coding specialist"
    "mistral-large-3|proxy|mistral-large-3|Mistral Large 3 675B — flagship MoE"
    "magistral-small|proxy|magistral-small|Magistral Small — reasoning model"
    "ministral-14b|proxy|ministral-14b|Ministral 14B — mid-size efficient"
    "ministral-8b|proxy|ministral-8b|Ministral 8B — fast, lightweight"
    "ministral-3b|proxy|ministral-3b|Ministral 3B — tiny, fastest"
    "voxtral-small-24b|proxy|voxtral-small-24b|Voxtral Small 24B — multimodal"
    "voxtral-mini-3b|proxy|voxtral-mini-3b|Voxtral Mini 3B — tiny multimodal"

    # ── Moonshot AI / Kimi (via Mantle) ───────────────────────────
    "kimi-k2.5|proxy|kimi-k2.5|Kimi K2.5 — coding + reasoning"
    "kimi-k2-thinking|proxy|kimi-k2-thinking|Kimi K2 Thinking — chain-of-thought"

    # ── MiniMax (via Mantle) ──────────────────────────────────────
    "minimax-m2|proxy|minimax-m2|MiniMax M2 — general purpose"
    "minimax-m2.1|proxy|minimax-m2.1|MiniMax M2.1 — improved general"
    "minimax-m2.5|proxy|minimax-m2.5|MiniMax M2.5 — latest, 80.2% SWE-bench"

    # ── NVIDIA Nemotron (via Mantle) ──────────────────────────────
    "nemotron-super-120b|proxy|nemotron-super-120b|Nemotron Super 120B — large reasoning"
    "nemotron-nano-30b|proxy|nemotron-nano-30b|Nemotron Nano 30B — mid-size"
    "nemotron-nano-12b|proxy|nemotron-nano-12b|Nemotron Nano 12B — compact"
    "nemotron-nano-9b|proxy|nemotron-nano-9b|Nemotron Nano 9B — smallest"

    # ── OpenAI GPT OSS (via Mantle) ──────────────────────────────
    "gpt-oss-120b|proxy|gpt-oss-120b|GPT OSS 120B — open-source GPT"
    "gpt-oss-20b|proxy|gpt-oss-20b|GPT OSS 20B — compact open-source GPT"
    "gpt-oss-safeguard-120b|proxy|gpt-oss-safeguard-120b|GPT OSS Safeguard 120B"
    "gpt-oss-safeguard-20b|proxy|gpt-oss-safeguard-20b|GPT OSS Safeguard 20B"

    # ── Z.AI / GLM (via Mantle) ──────────────────────────────────
    "glm-5|proxy|glm-5|GLM 5 — latest general model"
    "glm-4.7|proxy|glm-4.7|GLM 4.7 — strong reasoning"
    "glm-4.7-flash|proxy|glm-4.7-flash|GLM 4.7 Flash — fast inference"
    "glm-4.6|proxy|glm-4.6|GLM 4.6 — previous gen"

    # ── Google Gemma (via Mantle) ─────────────────────────────────
    "gemma-3-27b|proxy|gemma-3-27b|Gemma 3 27B — open model, largest"
    "gemma-3-12b|proxy|gemma-3-12b|Gemma 3 12B — open model, mid-size"
    "gemma-3-4b|proxy|gemma-3-4b|Gemma 3 4B — open model, compact"

    # ── Writer / Palmyra (via Mantle) ─────────────────────────────
    "palmyra-vision-7b|proxy|palmyra-vision-7b|Palmyra Vision 7B — vision model"

    # ── Self-hosted via Ollama (proxy required, SSH tunnel must be active) ──
    # Uncomment after starting tunnel: ./scripts/tunnel.sh start
    # "qwen-local|proxy|qwen-local|Qwen 3.5 35B — self-hosted on GPU server"
)

# ── Functions ─────────────────────────────────────────────────────

list_models() {
    echo ""
    echo "Available Models for Claude Code + Bedrock"
    echo "==========================================="
    echo ""
    echo "Backend: Bedrock Mantle (Chat Completions API) — all proxy models support tools + streaming"
    echo ""
    printf "  %-24s %-8s %s\n" "ALIAS" "TYPE" "DESCRIPTION"
    printf "  %-24s %-8s %s\n" "-----" "----" "-----------"

    local current_section=""
    for entry in "${MODELS[@]}"; do
        IFS='|' read -r alias type model_id desc <<< "$entry"
        printf "  %-24s %-8s %s\n" "$alias" "$type" "$desc"
    done
    echo ""
    echo "native = direct Bedrock (no proxy needed, Anthropic models only)"
    echo "proxy  = via LiteLLM proxy -> Bedrock Mantle (start with: ./scripts/setup-proxy.sh)"
    echo ""
    echo "Total: ${#MODELS[@]} models (5 native + $((${#MODELS[@]} - 5)) via Mantle)"
}

lookup_model() {
    local search="$1"
    for entry in "${MODELS[@]}"; do
        IFS='|' read -r alias type model_id desc <<< "$entry"
        if [[ "$alias" == "$search" ]]; then
            echo "$alias|$type|$model_id|$desc"
            return 0
        fi
    done
    return 1
}

pick_model_interactive() {
    echo "" >&2
    echo "Select a model:" >&2
    echo "" >&2
    local i=1
    for entry in "${MODELS[@]}"; do
        IFS='|' read -r alias type model_id desc <<< "$entry"
        printf "  %2d) %-24s [%s] %s\n" "$i" "$alias" "$type" "$desc" >&2
        ((i++))
    done
    echo "" >&2
    read -rp "Enter number (1-${#MODELS[@]}): " choice

    if [[ "$choice" -ge 1 && "$choice" -le "${#MODELS[@]}" ]]; then
        echo "${MODELS[$((choice-1))]}"
    else
        echo "[error] Invalid choice" >&2
        exit 1
    fi
}

check_proxy() {
    if ! curl -sf "http://localhost:${PROXY_PORT}/health" &>/dev/null; then
        echo "[error] LiteLLM proxy not running on port $PROXY_PORT"
        echo "        Start it: ./scripts/setup-proxy.sh"
        exit 1
    fi
}

# ── Parse args ────────────────────────────────────────────────────

MODEL_ALIAS=""
CLAUDE_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --model|-m)  MODEL_ALIAS="$2"; shift 2 ;;
        --list|-l)   list_models; exit 0 ;;
        -h|--help)
            echo "Usage: $0 [--model ALIAS] [--list] [claude args...]"
            echo "       $0 --model qwen-coder-next -p 'write a function'"
            echo "       $0 --list"
            exit 0
            ;;
        *)  CLAUDE_ARGS+=("$1"); shift ;;
    esac
done

# Interactive selection if no model specified
if [[ -z "$MODEL_ALIAS" ]]; then
    SELECTED=$(pick_model_interactive)
else
    SELECTED=$(lookup_model "$MODEL_ALIAS") || {
        echo "[error] Unknown model: $MODEL_ALIAS"
        echo "        Run: $0 --list"
        exit 1
    }
fi

IFS='|' read -r ALIAS TYPE MODEL_ID DESC <<< "$SELECTED"
echo ""
echo "[model] $ALIAS — $DESC"

# ── Launch Claude Code ────────────────────────────────────────────

if [[ "$TYPE" == "native" ]]; then
    echo "[mode] Native Bedrock (no proxy)"
    echo ""
    CLAUDE_CODE_USE_BEDROCK=1 \
    AWS_REGION="$AWS_REGION" \
    ANTHROPIC_MODEL="$MODEL_ID" \
    claude ${CLAUDE_ARGS[@]+"${CLAUDE_ARGS[@]}"}

elif [[ "$TYPE" == "proxy" ]]; then
    check_proxy
    echo "[mode] LiteLLM proxy -> Bedrock Mantle (localhost:$PROXY_PORT)"
    echo ""
    ANTHROPIC_BASE_URL="http://localhost:${PROXY_PORT}" \
    ANTHROPIC_API_KEY="bedrock-proxy" \
    claude --settings "$PROJECT_DIR/config/claude-proxy-settings.json" \
           --model "$MODEL_ID" \
           ${CLAUDE_ARGS[@]+"${CLAUDE_ARGS[@]}"}
fi
