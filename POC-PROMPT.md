# PoC Prompt: Looking Beyond Anthropic Models for Claude Code

## Objective
Build a show-and-tell PoC demonstrating that Claude Code (and OpenCode) can run effectively with cost-optimized non-Anthropic models on Amazon Bedrock, targeting 55-70% blended cost reduction without meaningful quality degradation.

## The Idea
Claude Code and OpenCode are coding agent harnesses that can be paired with models beyond their defaults. We benchmark both harnesses with frontier models (Claude Opus, GPT-5.2) and cost-optimized alternatives: Kimi K2 (1T params, 5x cheaper), MiniMax M2.5 (80.2% SWE-bench, 10x cheaper), and Qwen3 Coder (256K context, 20x cheaper). OpenCode achieves 42-43% pass rates with frontier models vs Claude Code at 48.2%, but the performance gap narrows on routine tasks. This PoC explores harness-model combinations across developer profiles to find the cost-quality sweet spot.

## Architecture
```
Claude Code (Anthropic Messages API)
    → LiteLLM Proxy (localhost:4000, translates Anthropic → OpenAI format)
        → Bedrock Mantle (bedrock-mantle.us-east-1.api.aws, Chat Completions API)
            → Any of 38 non-Anthropic models from 12 providers
```

Why LiteLLM is needed: Claude Code only speaks Anthropic Messages API. Bedrock Mantle speaks OpenAI Chat Completions API for non-Anthropic models. LiteLLM translates between them.

## What Has Been Done

### Phase 1: Mantle Backend (Complete)
- Switched from bedrock-runtime (Converse API) to Bedrock Mantle (Chat Completions API)
- All 38 non-Anthropic Mantle models configured and tested via Anthropic Messages API
- Bearer token auth via aws-bedrock-token-generator (12h validity, auto-generated)
- LiteLLM routes: Anthropic Messages API → OpenAI Chat Completions → Mantle
- Key fix: `LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true` (forces chat completions over responses API)
- Token region fix: forced us-east-1 scope regardless of AWS_REGION env var
- Tools + streaming confirmed working for ALL 38 models across 12 providers

### Models Tested End-to-End (38 via Mantle, 5 native)
**Via Mantle (all support tools + streaming):**
- Qwen: Coder Next, Coder 480B, Coder 30B, 235B, 32B, VL 235B, Next 80B
- DeepSeek: V3.2, V3.1
- Mistral: Devstral 123B, Large 3 675B, Magistral Small, Ministral 14B/8B/3B, Voxtral Small/Mini
- Moonshot: Kimi K2.5, K2 Thinking
- MiniMax: M2, M2.1, M2.5
- NVIDIA: Nemotron Super 120B, Nano 30B/12B/9B
- OpenAI: GPT OSS 120B/20B, Safeguard 120B/20B
- Z.AI: GLM 5, 4.7, 4.7 Flash, 4.6
- Google: Gemma 3 27B/12B/4B
- Writer: Palmyra Vision 7B

**Native Bedrock (no proxy):**
- Claude Opus 4.6, Sonnet 4.6, Haiku 4.5, Opus 4.5, Sonnet 4.5

### Infrastructure
- LiteLLM proxy config with 38 Mantle models (litellm-config.yaml)
- Setup script (setup-proxy.sh) — token generation, LiteLLM install, proxy start, health checks
- Token refresh script (mantle-token.sh) — standalone bearer token generation
- Model picker script (claude-model.sh) — interactive menu with 43 models (38 proxy + 5 native)
- Tested on Mac locally and fresh EC2 instance (us-east-1)
- Pushed to GitHub: shekharprateek/claude-code-on-amazon-ec2/multi-model/

### Key Discoveries
- Bedrock Mantle has 40 models (38 non-Anthropic + 2 Anthropic) — all support tools + streaming
- Meta Llama and Amazon Nova are NOT on Mantle (bedrock-runtime only, no tool support)
- DeepSeek R1 is NOT on Mantle (bedrock-runtime only)
- Claude Mythos Preview is Mantle-only (new model)
- LiteLLM v1.83+ defaults to /v1/responses for openai/ provider — must force chat completions

### Known Issues Fixed
- Corrected 6+ wrong Bedrock model IDs
- Fixed LiteLLM routing: openai/ provider was sending to /v1/responses instead of /v1/chat/completions
- Fixed token region scoping: AWS_REGION env var override was generating us-west-2 tokens for us-east-1 endpoint
- Fixed IAM instance profile credential detection in setup script
- Fixed Claude Code model validation conflict (CLAUDE_CODE_USE_BEDROCK=0 override needed)
- Removed Llama/Nova models (not available on Mantle, no tool support on bedrock-runtime)

## What Needs To Be Done

### Phase 1: Switch to Mantle Backend (COMPLETE)
- [x] Update litellm-config.yaml to route through Mantle instead of bedrock-runtime
- [x] Add all 38 non-Anthropic Mantle models to the config (40 total on Mantle, 2 are Anthropic)
- [x] Test all models end-to-end via Anthropic Messages API (LiteLLM → Mantle)
- [x] Verify tools + streaming works for all models (confirmed via tool_use test)
- [x] Note: Llama/Nova are NOT on Mantle (bedrock-runtime only) — removed from config

### Phase 2: Benchmark Framework
- [ ] Create a benchmark script that runs each model against standardized coding tasks
- [ ] Task categories: bug fix, feature addition, refactoring, code review, test writing
- [ ] Measure per model: pass rate, latency (time to first token, total time), output quality
- [ ] Run 5-10 tasks per model minimum for statistical significance

### Phase 3: Cost Analysis
- [ ] Pull per-model pricing from Bedrock (input/output token costs)
- [ ] Calculate cost per task for each model
- [ ] Generate cost-quality tradeoff chart
- [ ] Identify the sweet spot: which models give 55-70% cost reduction with <5% quality drop

### Phase 4: OpenCode Comparison
- [ ] Set up OpenCode harness (separate from Claude Code)
- [ ] Run same benchmark tasks through OpenCode with same models
- [ ] Compare pass rates: Claude Code vs OpenCode per model
- [ ] Identify where OpenCode narrows the gap on routine tasks

### Phase 5: Show-and-Tell Deliverables
- [ ] Architecture diagram (Claude Code → LiteLLM → Mantle → Models)
- [ ] Model comparison table (39 models, pass rate, latency, cost)
- [ ] Cost-quality scatter plot
- [ ] Live demo: switch models on the fly during a coding task
- [ ] Recommendation: which model for which developer profile (senior/junior, routine/complex)

## How to Run (Current State)

```bash
# Start the proxy
cd ~/claude-code-multi-model-bedrock
./scripts/setup-proxy.sh

# Run Claude Code with any model (recommended — uses --settings to override Bedrock mode)
./scripts/claude-model.sh --model qwen-coder-next
./scripts/claude-model.sh --model deepseek-v3
./scripts/claude-model.sh --model kimi-k2.5

# Or use the interactive picker
./scripts/claude-model.sh

# Manual (requires --settings to disable Bedrock mode if CLAUDE_CODE_USE_BEDROCK=1 is in settings.json)
ANTHROPIC_BASE_URL=http://localhost:4000 \
ANTHROPIC_API_KEY=bedrock-proxy \
claude --settings ~/claude-code-multi-model-bedrock/config/claude-proxy-settings.json \
       --model deepseek-v3
```

## Key Files
- Config: ~/claude-code-multi-model-bedrock/config/litellm-config.yaml (38 Mantle models)
- Setup: ~/claude-code-multi-model-bedrock/scripts/setup-proxy.sh (token gen + proxy start)
- Token: ~/claude-code-multi-model-bedrock/scripts/mantle-token.sh (standalone bearer token)
- Picker: ~/claude-code-multi-model-bedrock/scripts/claude-model.sh (43 models: 38 proxy + 5 native)
- GitHub: shekharprateek/claude-code-multi-model-bedrock
