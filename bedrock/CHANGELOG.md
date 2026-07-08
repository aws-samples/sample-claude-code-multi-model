# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Support for GPT-5.5 and GPT-5.4 (`gpt-5.5`, `gpt-5.4`) via the LiteLLM proxy
- `model_info.mode: responses` routing for GPT-5.5/GPT-5.4 in `config/litellm-config.yaml`, since these models only support the OpenAI Responses API on `bedrock-mantle` (Chat Completions returns a 400)

## [1.0.0] - 2026-05-04

### Added
- Initial release: run Claude Code with 43 models (38 non-Anthropic + 5 Anthropic) on Amazon Bedrock
- LiteLLM proxy translates Anthropic Messages API to the Amazon Bedrock Chat Completions API
- Bearer token authentication via aws-bedrock-token-generator (12h validity)
- Interactive model picker (`scripts/claude-model.sh`) with 43 models from 12 providers
- One-command proxy setup (`scripts/setup-proxy.sh`) with token generation and health checks
- Standalone token generator (`scripts/mantle-token.sh`)
- Support for Qwen, DeepSeek, Mistral, Moonshot, MiniMax, NVIDIA, OpenAI, Z.AI, Google, and Writer models
