# SWE Benchmark

A software-engineering benchmark that drives a coding assistant (Claude Code, or any agent) through real-world tasks **non-interactively** and measures both the quality of what it produces and what it cost to produce.

Each task points the agent at a GitHub repository and a problem to solve. The agent works the task through the `/swe` skill, which lands four design artifacts on disk (`github-issue.md`, `lld.md`, `review.md`, `testing.md`). A separate review pass then scores those artifacts, while the harness records per-run instrumentation: token usage, latency, and the number of LLM turns (calls to the model) the agent took.

This README covers the pieces that exist today: the **dataset format**, the **dataset loader**, the **runner config**, and the **headless run harness** that drives `claude -p` through the `/swe` skill for each task. The reviewer (the separate pass that scores the artifacts) is built on top of these and is documented as it lands.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for package and environment management.
- Python 3.10+.

The benchmark harness has its own virtual environment, isolated from the model runtimes elsewhere in this repository. Set it up once:

```bash
cd benchmarks
uv sync
```

This creates `benchmarks/.venv` with `pydantic`, `pyyaml`, `requests`, `matplotlib`, and `numpy`, plus the dev tools (`ruff`, `mypy`, `bandit`). Run everything in this directory with `uv run`.

## The dataset

A dataset is a single YAML file: a metadata header plus a list of tasks. Datasets live in [dataset/](dataset/); the reference dataset is [dataset/mcp-gateway-registry.yaml](dataset/mcp-gateway-registry.yaml), whose tasks are drawn from real upstream issues in [agentic-community/mcp-gateway-registry](https://github.com/agentic-community/mcp-gateway-registry).

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | str | Version of the file format. The loader only accepts versions it knows about. |
| `name` | str | Machine-friendly dataset id (kebab-case). |
| `title` | str | Human-readable dataset name. |
| `description` | str | What the dataset covers and how it is meant to run. |
| `created` | date | ISO date the dataset was authored (`YYYY-MM-DD`). Optional. |
| `default_ref` | str | Git ref (tag, branch, or commit) a task clones when it does not set its own `ref`. Pin this for reproducibility. |
| `metrics` | list | The per-run signals the harness is expected to collect. Documentary only; actual values live in run outputs, never in the dataset. |
| `complexity_levels` | list | The allowed values for a task's `complexity` field. |
| `tasks` | list | The tasks (see below). |

### Per-task fields

| Field | Type | Description |
| --- | --- | --- |
| `id` | str | Stable slug; also used as the output subdirectory name. Must be unique within the dataset. |
| `repo` | str | HTTPS URL of the repository the agent clones. |
| `ref` | str | Git ref for this task. Optional; defaults to `default_ref`. Always pin so runs are reproducible. |
| `complexity` | str | One of `complexity_levels`. |
| `tags` | list | Free-form labels for slicing results (domain, language, change type, AWS service, and so on). |
| `problem_statement` | str | Multi-line description of the task, in enough detail for the agent to act without the repo author present. |
| `problem_issue_url` | str | Canonical GitHub issue the task derives from. When both this and `problem_statement` are present, the statement is authoritative and the URL is the source of record. |
| `clarifying_answers` | str | Pre-supplied answers to the questions the `/swe` skill would otherwise ask, so the run stays fully non-interactive. Optional. |
| `ground_truth` | map | Reviewer-facing notes on the intended solution. **Never given to the agent.** Optional. |

At least one of `problem_statement` or `problem_issue_url` must be present on every task.

`ground_truth`, when present, has three sub-fields, all optional:

| Field | Type | Description |
| --- | --- | --- |
| `approach` | str | How the change is meant to be made. |
| `expectations` | list | Points a correct design is expected to cover. |
| `reference_url` | str | How the issue was actually resolved upstream (PR, commit, or issue), if known. |

### Minimal example

```yaml
schema_version: "1.0"
name: example-dataset
title: Example dataset
description: A minimal valid dataset.
default_ref: main
metrics: [input_tokens, output_tokens, num_turns]
complexity_levels: [low, medium, high]
tasks:
  - id: fix-the-thing
    repo: https://github.com/example/repo
    complexity: low
    tags: [demo]
    problem_statement: |
      Describe the task here, in enough detail for an agent to act on it.
```

## The dataset loader

[scripts/dataset_loader.py](scripts/dataset_loader.py) parses a dataset file into typed [Pydantic](https://docs.pydantic.dev/) models and validates it, so every consumer reads the same enforced shape instead of poking at raw dictionaries.

It exposes three models -- `Dataset`, `Task`, and `GroundTruth` -- and a single entry point:

```python
from dataset_loader import load_dataset

dataset = load_dataset("dataset/hello-world.yaml")
for task in dataset.tasks:
    print(task.id, task.complexity, dataset.resolved_ref(task))

task = dataset.task_by_id("add-contributing-guide")
```

`load_dataset` raises `DatasetError` if the file is missing, unparseable, or fails validation. Validation enforces:

- The `schema_version` is one the loader supports.
- Every task's `complexity` is one of the dataset's `complexity_levels`.
- Task ids are unique.
- Every task has at least one problem source (`problem_statement` or `problem_issue_url`).

The loader also resolves each task's `ref` to `default_ref` when the task omits it, so downstream code always sees a concrete ref.

### Validating a dataset from the command line

The loader doubles as a CLI that validates a file and prints a summary -- useful when authoring or editing a dataset:

```bash
cd benchmarks
uv run scripts/dataset_loader.py dataset/hello-world.yaml
```

It exits non-zero and logs the validation error if the file is invalid.

## The runner config

The dataset says *what* to run; the runner config says *how* to run it. It is a small YAML file ([config/runner.example.yaml](config/runner.example.yaml)) holding the run-time parameters: which endpoint and model to drive, which dataset to run, where artifacts go, and how `claude -p` is invoked. [scripts/runner_config.py](scripts/runner_config.py) parses it into a validated Pydantic `RunnerConfig`.

Every field can be overridden on the command line, and **CLI flags always win over the file**, so a committed config stays the reusable default while one-off runs stay flexible.

### Setup: create your own config

`config/runner.example.yaml` is a template, not a config you run directly. Before your first run, copy it to `config/runner.yaml` and edit it for your endpoint and model:

```bash
cd benchmarks
cp config/runner.example.yaml config/runner.yaml
# then edit config/runner.yaml: set endpoint, model, and dataset
```

`config/runner.yaml` is gitignored, so your local endpoint, key, and model choices never get committed. Keep `config/runner.example.yaml` as the checked-in, documented template; point the harness at your copy with `--config config/runner.yaml`. Leave `settings_file` commented out unless you specifically need the options in the vLLM settings file -- the harness synthesizes routing from `endpoint` and `api_key` on its own (see [How routing to the endpoint works](#how-routing-to-the-endpoint-works)).

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `endpoint` | str | -- | Base URL of the OpenAI/Anthropic-compatible endpoint the model is served on. |
| `model` | str | -- | Model name/id passed to `claude --model`; also the `{model-name}` artifact subfolder. |
| `api_key` | str | `local` | API key sent to the endpoint (local servers ignore the value). |
| `dataset` | str | -- | Path to the dataset YAML (relative to `benchmarks/`). |
| `output_dir` | str | `swe-benchmark-data` | Directory (under `benchmarks/`) where the `/swe` skill writes artifacts. |
| `clone_dir` | str | `/tmp` | Parent directory for per-task temporary repo clones. |
| `tasks` | list | `[]` | Task ids to run; empty runs every task in the dataset. |
| `permission_mode` | str | `acceptEdits` | `claude -p` permission mode. `bypassPermissions` is intentionally rejected. |
| `allowed_tools` | list | read + write set | Tools `claude -p` may use without prompting. |
| `max_turns` | int | `60` | Cap on the agent loop (`claude --max-turns`). |
| `max_output_tokens` | int | `16000` | Per-response output-token cap. |
| `timeout_seconds` | int | `1800` | Wall-clock timeout for a single task's run. |
| `settings_file` | str | none | Optional `claude --settings` JSON (e.g. the vLLM Claude Code config). |

Validate a config the same way as a dataset:

```bash
cd benchmarks
uv run scripts/runner_config.py config/runner.example.yaml
```

## Running the benchmark

[scripts/run-swe-headless.py](scripts/run-swe-headless.py) is the harness. For each selected task it:

1. Clones the task's repo at its pinned ref into a temporary directory under `clone_dir`.
2. Invokes `claude -p "/swe repo: ... problem: ... model: ... answers: ..."` non-interactively, letting the `/swe` skill produce the four artifacts under `swe-benchmark-data/{repo-name}/{task-id}/{model-name}/`.
3. Parses the run's JSON result (`--output-format json`) for the six benchmark metrics -- input, output, cache-read, and cache-creation tokens; latency; and `num_turns` -- and writes them to `metrics.json` beside the artifacts.
4. Removes the temporary clone.

It runs `claude -p` with `--permission-mode acceptEdits` and a narrow `--allowedTools` allowlist; it never uses `bypassPermissions` or `--dangerously-skip-permissions`.

### How routing to the endpoint works

The harness always passes `claude --settings`, and this matters: a Claude Code settings object's `env` block takes precedence over process environment variables, including any in your global `~/.claude/settings.json`. If that global file pins `CLAUDE_CODE_USE_BEDROCK=1` (a common setup), then merely exporting `CLAUDE_CODE_USE_BEDROCK=0` before the run is silently overridden, the request goes to Amazon Bedrock instead of your endpoint, and Bedrock rejects the local model id with `400 The provided model identifier is invalid`. Passing `--settings` is what reliably wins over the global file.

The harness sources the settings two ways:

- If the config sets `settings_file`, it passes that file (e.g. the vLLM `self-hosted/vllm/config/claude-code.json`).
- Otherwise it synthesizes an inline settings object from the config's `endpoint` and `api_key`, pinning `CLAUDE_CODE_USE_BEDROCK=0`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, and an `apiKeyHelper`.

The `apiKeyHelper` is required even against a local server that ignores the key's value: without a token source Claude Code aborts with `Not logged in - Please run /login`. The synthesized settings set it to `echo <api_key>`, so the config's `api_key` field doubles as that token.

Because routing is synthesized from `endpoint`/`api_key`, runs work with `settings_file` left unset (commented out) -- keep it set only when you need the extra options in the vLLM settings file.

When `claude -p` returns an error, the harness records the error message and `api_error_status` in `metrics.json` and logs them, so a failed run is diagnosable without re-running it by hand.

```bash
cd benchmarks

# Run every task in the dataset named by the config
uv run scripts/run-swe-headless.py --config config/runner.yaml

# Kick the tires: run the trivial hello-world sanity dataset first
uv run scripts/run-swe-headless.py --config config/runner.yaml \
    --dataset dataset/hello-world.yaml

# Override the model and run a subset of tasks (CLI wins over the config)
uv run scripts/run-swe-headless.py --config config/runner.yaml \
    --dataset dataset/hello-world.yaml --model qwen3-coder-30b \
    --tasks add-contributing-guide

# Smoke-test a large dataset by running only its first task
uv run scripts/run-swe-headless.py --config config/runner.yaml \
    --dataset dataset/mcp-gateway-registry.yaml --count 1

# Print the prompt and command for each task without running anything
uv run scripts/run-swe-headless.py --config config/runner.yaml --dry-run
```

`--count N` keeps only the first `N` tasks in dataset order (after any `--tasks` filter); `--count 0`, the default, runs them all.

[scripts/run-swe-benchmark.sh](scripts/run-swe-benchmark.sh) is a thin convenience wrapper that forwards its arguments to the harness, injecting `--config config/runner.yaml` when you do not pass your own `--config`:

```bash
cd benchmarks
./scripts/run-swe-benchmark.sh --dataset dataset/hello-world.yaml --dry-run
```

Before running against a live endpoint, make sure the model is served and reachable at the config's `endpoint` (for the vLLM path, see [self-hosted/vllm/README.md](../self-hosted/vllm/README.md)).

### The metrics file

Alongside the four artifacts, each run writes a `metrics.json` capturing what the run cost and whether it produced everything expected. Here is a real example from the hello-world sanity run:

```json
{
  "task": "add-contributing-guide",
  "repo": "https://github.com/octocat/Hello-World",
  "ref": "master",
  "complexity": "low",
  "tags": [
    "sanity-check",
    "docs",
    "hello-world"
  ],
  "model": "qwen3.6-35b",
  "endpoint": "http://127.0.0.1:8000",
  "artifacts_produced": 4,
  "artifacts_expected": 4,
  "generation_tokens_per_sec": 124.0,
  "input_tokens": 364184,
  "output_tokens": 9388,
  "cache_read_tokens": 0,
  "cache_creation_tokens": 0,
  "latency_seconds": 75.7,
  "num_turns": 17,
  "total_cost_usd": 2.058595,
  "is_error": false,
  "session_id": "78df4f2a-6598-4add-a253-287354c207bd"
}
```

Because the file records the task, model, and endpoint next to the token, latency, throughput, and turn-count numbers, the same task can be run against many models and the resulting `metrics.json` files compared side by side -- so you can see how each model differs in cost, speed, and how many turns it took to produce the artifacts. On a failed run the file also carries an `error` string and `api_error_status`, so a failure is diagnosable without re-running the task.

These metrics measure the *cost* of a run, not the *quality* of what it produced. A forthcoming eval skill will score the generated artifacts (against the dataset's `ground_truth`) and add those scoring results to this same `metrics.json`, so a single file will hold both what a run cost and how good its output was -- the basis for ranking models on the benchmark.

## Development workflow

Run these from the `benchmarks/` directory before committing:

```bash
uv run ruff check scripts/ tests/
uv run mypy scripts/dataset_loader.py scripts/runner_config.py
uv run bandit -r scripts/
uv run python -m unittest discover -s tests
```

## Repository layout

```
benchmarks/
├── config/             # Runner config YAML files (runner.example.yaml)
├── dataset/            # Benchmark dataset YAML files
├── scripts/            # Loaders, the run harness, and its shell wrapper
├── tests/              # Unit tests
├── pyproject.toml      # Dependencies and tooling config
└── README.md
```
