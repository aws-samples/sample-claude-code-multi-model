# Claude Code Multi-Model on Amazon Bedrock

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with **any foundation model on Amazon Bedrock** — not just Anthropic models. Switch between 43 models from 12 providers with a single command.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        Claude Code CLI                          │
│                  (speaks Anthropic Messages API)                │
└──────────┬──────────────────────────────────────────┬───────────┘
           │                                          │
   ┌───────▼────────┐                       ┌────────▼──────────┐
   │  Native Path   │                       │  LiteLLM Proxy    │
   │  (no proxy)    │                       │  (localhost:4000)  │
   │                │                       │                   │
   │  Claude Opus   │                       │  Anthropic →      │
   │  Claude Sonnet │                       │  OpenAI format    │
   │  Claude Haiku  │                       │  translation      │
   └───────┬────────┘                       └────────┬──────────┘
           │                                          │
   ┌───────▼────────┐                       ┌────────▼──────────┐
   │  Amazon        │                       │  Bedrock Mantle   │
   │  Bedrock       │                       │  (Chat Completions│
   │  (Anthropic)   │                       │   API, us-east-1) │
   │                │                       │                   │
   │                │                       │  38 models from   │
   │                │                       │  12 providers     │
   └────────────────┘                       └───────────────────┘
```

**Why a proxy?** Claude Code speaks the Anthropic Messages API (`/v1/messages`). Bedrock Mantle's third-party models speak the OpenAI Chat Completions API (`/v1/chat/completions`). [LiteLLM](https://github.com/BerriAI/litellm) translates between these formats.

**Why Mantle?** Bedrock Mantle is a unified OpenAI-compatible endpoint for non-Anthropic models on Bedrock. All 38 models support tool calling and streaming natively — no per-model configuration needed.

## Supported Models (43 total)

### Anthropic (5 — native Bedrock, no proxy)

| Alias | Model | Best For |
|-------|-------|----------|
| `claude-opus` | Claude Opus 4.6 | Flagship reasoning, complex tasks |
| `claude-sonnet` | Claude Sonnet 4.6 | Balanced speed/quality |
| `claude-haiku` | Claude Haiku 4.5 | Fast, lightweight tasks |
| `claude-opus-4.5` | Claude Opus 4.5 | Previous gen flagship |
| `claude-sonnet-4.5` | Claude Sonnet 4.5 | Previous gen balanced |

### Third-Party (38 — via LiteLLM proxy → Bedrock Mantle)

| Provider | Models | Aliases |
|----------|--------|---------|
| **Qwen** (7) | Coder Next, Coder 480B, Coder 30B, 235B, 32B, VL 235B, Next 80B | `qwen-coder-next`, `qwen-coder-480b`, `qwen-coder-30b`, `qwen-235b`, `qwen-32b`, `qwen-vl-235b`, `qwen-next-80b` |
| **DeepSeek** (2) | V3.2, V3.1 | `deepseek-v3`, `deepseek-v3.1` |
| **Mistral** (8) | Devstral 123B, Large 3 675B, Magistral Small, Ministral 14B/8B/3B, Voxtral Small/Mini | `devstral-123b`, `mistral-large-3`, `magistral-small`, `ministral-14b`, `ministral-8b`, `ministral-3b`, `voxtral-small-24b`, `voxtral-mini-3b` |
| **Moonshot AI** (2) | Kimi K2.5, K2 Thinking | `kimi-k2.5`, `kimi-k2-thinking` |
| **MiniMax** (3) | M2, M2.1, M2.5 | `minimax-m2`, `minimax-m2.1`, `minimax-m2.5` |
| **NVIDIA** (4) | Nemotron Super 120B, Nano 30B/12B/9B | `nemotron-super-120b`, `nemotron-nano-30b`, `nemotron-nano-12b`, `nemotron-nano-9b` |
| **OpenAI** (4) | GPT OSS 120B/20B, Safeguard 120B/20B | `gpt-oss-120b`, `gpt-oss-20b`, `gpt-oss-safeguard-120b`, `gpt-oss-safeguard-20b` |
| **Z.AI** (4) | GLM 5, 4.7, 4.7 Flash, 4.6 | `glm-5`, `glm-4.7`, `glm-4.7-flash`, `glm-4.6` |
| **Google** (3) | Gemma 3 27B/12B/4B | `gemma-3-27b`, `gemma-3-12b`, `gemma-3-4b` |
| **Writer** (1) | Palmyra Vision 7B | `palmyra-vision-7b` |

> **Note:** Meta Llama, Amazon Nova, and DeepSeek R1 are available on Bedrock but are **not** on Mantle — they lack tool calling support required by Claude Code.

## Prerequisites

- **AWS Account** with Bedrock model access enabled
- **AWS CLI** configured (`aws configure` or IAM role/SSO)
- **Python 3.9+** (for LiteLLM proxy and token generation)
- **Claude Code CLI** installed ([docs](https://docs.anthropic.com/en/docs/claude-code))

## Quick Start

### 1. Clone and setup

```bash
git clone https://github.com/shekharprateek/claude-code-multi-model-bedrock.git
cd claude-code-multi-model-bedrock
chmod +x scripts/*.sh
```

### 2. Use Anthropic models (no proxy needed)

```bash
./scripts/claude-model.sh --model claude-opus
./scripts/claude-model.sh --model claude-sonnet
./scripts/claude-model.sh --model claude-haiku
```

### 3. Use third-party models (proxy required)

```bash
# Step 1: Start the LiteLLM proxy (generates Mantle token, installs deps)
./scripts/setup-proxy.sh

# Step 2: Run Claude Code with any model
./scripts/claude-model.sh --model qwen-coder-next
./scripts/claude-model.sh --model deepseek-v3
./scripts/claude-model.sh --model kimi-k2.5
./scripts/claude-model.sh --model devstral-123b

# With a prompt
./scripts/claude-model.sh --model qwen-coder-next -p "write a Python REST API"
```

### 4. Interactive model picker

```bash
./scripts/claude-model.sh
# Shows numbered list of all 43 models — pick one
```

### 5. List all available models

```bash
./scripts/claude-model.sh --list
```

## Proxy Management

```bash
# Start proxy (installs litellm + token generator if needed)
./scripts/setup-proxy.sh

# Custom port
./scripts/setup-proxy.sh --port 8080

# Check status
./scripts/setup-proxy.sh --status

# Refresh Mantle bearer token (valid 12h)
./scripts/setup-proxy.sh --refresh

# Stop proxy
./scripts/setup-proxy.sh --stop

# View logs
tail -f .litellm.log
```

## Manual Configuration (No Scripts)

### Anthropic models (native Bedrock)

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
claude
```

### Third-party models (via proxy)

```bash
# Terminal 1: Start proxy
pip install "litellm[proxy]" aws-bedrock-token-generator
eval $(./scripts/mantle-token.sh)
LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true \
  litellm --config config/litellm-config.yaml --port 4000

# Terminal 2: Run Claude Code
ANTHROPIC_BASE_URL=http://localhost:4000 \
ANTHROPIC_API_KEY=bedrock-proxy \
claude --settings config/claude-proxy-settings.json \
       --model qwen-coder-next
```

> **Important:** The `--settings config/claude-proxy-settings.json` flag disables Bedrock native mode (`CLAUDE_CODE_USE_BEDROCK=0`) so Claude Code routes through the proxy instead. Without it, Claude Code may try to connect directly to Bedrock and fail for non-Anthropic model IDs.

## Shell Aliases (Optional)

Add to `~/.zshrc` or `~/.bashrc`:

```bash
# Native Bedrock models
alias cc-opus='CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1 claude'
alias cc-sonnet='CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1 claude'

# Proxy models (requires LiteLLM running on :4000)
CC_PROXY="ANTHROPIC_BASE_URL=http://localhost:4000 ANTHROPIC_API_KEY=bedrock-proxy"
alias cc-qwen="$CC_PROXY claude --settings ~/claude-code-multi-model-bedrock/config/claude-proxy-settings.json --model qwen-coder-next"
alias cc-deepseek="$CC_PROXY claude --settings ~/claude-code-multi-model-bedrock/config/claude-proxy-settings.json --model deepseek-v3"
alias cc-devstral="$CC_PROXY claude --settings ~/claude-code-multi-model-bedrock/config/claude-proxy-settings.json --model devstral-123b"
alias cc-kimi="$CC_PROXY claude --settings ~/claude-code-multi-model-bedrock/config/claude-proxy-settings.json --model kimi-k2.5"
```

## Key Files

| File | Purpose |
|------|---------|
| `config/litellm-config.yaml` | LiteLLM proxy config — all 38 Mantle models |
| `config/claude-proxy-settings.json` | Claude Code settings override (disables native Bedrock mode) |
| `scripts/setup-proxy.sh` | One-command proxy setup (token gen + install + start) |
| `scripts/claude-model.sh` | Model picker / launcher (43 models) |
| `scripts/mantle-token.sh` | Standalone Mantle bearer token generator |

## How It Works

1. **Token generation**: `setup-proxy.sh` generates a bearer token from your AWS IAM credentials using `aws-bedrock-token-generator`. Tokens are scoped to `us-east-1` and valid for 12 hours.

2. **LiteLLM translation**: The proxy receives Anthropic Messages API requests from Claude Code and translates them to OpenAI Chat Completions format for Bedrock Mantle.

3. **Bedrock Mantle**: AWS's unified endpoint (`bedrock-mantle.us-east-1.api.aws`) routes requests to the selected model. All 38 non-Anthropic models support tool calling and streaming.

4. **Key env var**: `LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true` forces LiteLLM to use `/v1/chat/completions` (not `/v1/responses`) — required for Mantle compatibility with LiteLLM v1.83+.

## Limitations

- **Context window**: Third-party models have smaller context windows (128K or less) compared to Claude's 200K. Claude Code's system prompt is large (~100K chars), so very small models may not work well.
- **Tool calling quality**: Claude Code relies heavily on structured tool use. Non-Anthropic models vary in tool-calling reliability.
- **Prompt caching**: Disabled for proxy models (not supported across the translation layer).
- **Region**: Bedrock Mantle is currently only available in `us-east-1`.
- **Token expiry**: Mantle bearer tokens expire after 12 hours. Use `./scripts/setup-proxy.sh --refresh` to regenerate.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Proxy not reachable` | Run `./scripts/setup-proxy.sh` |
| `AccessDeniedException` | Enable model access in [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess) |
| `AWS credentials not configured` | Run `aws configure` or set up IAM role/SSO |
| `The provided model identifier is invalid` | Make sure you're using `--settings config/claude-proxy-settings.json` (disables native Bedrock mode) |
| `Token expired` | Run `./scripts/setup-proxy.sh --refresh` then restart proxy |
| Small model fails with Claude Code | Claude Code's system prompt is ~100K chars — models with <128K context may fail |

## License

MIT
