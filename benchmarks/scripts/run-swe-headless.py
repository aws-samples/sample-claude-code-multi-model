#!/usr/bin/env python3
"""Run the SWE benchmark headless: drive `claude -p /swe` over a dataset.

Given a dataset YAML and a runner config (endpoint, model, claude flags), this
harness runs each task end to end:

  1. Clone the task's repo at its pinned ref into a temporary directory.
  2. Invoke `claude -p "/swe repo: ... problem: ... model: ... answers: ..."`
     non-interactively, letting the /swe skill produce the four design
     artifacts (github-issue.md, lld.md, review.md, testing.md).
  3. Parse the run's JSON result for the six benchmark metrics (input/output/
     cache tokens, latency, and the number of LLM turns the agent took) and
     write them to metrics.json next to the artifacts.

Routing and claude flags come from the runner config; any field may be
overridden on the command line (CLI wins).

Usage:
    uv run scripts/run-swe-headless.py --config config/runner.example.yaml
    uv run scripts/run-swe-headless.py --config config/runner.example.yaml \\
        --model qwen3-coder-30b --tasks remove-faiss
    uv run scripts/run-swe-headless.py --config config/runner.example.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - used with list args, no shell, hardcoded command
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dataset_loader import Dataset, DatasetError, Task, load_dataset
from runner_config import RunnerConfig, RunnerConfigError, load_runner_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACT_FILENAMES = ("github-issue.md", "lld.md", "review.md", "testing.md")
GIT_CLONE_TIMEOUT_SECONDS = 300


def _repo_name(repo_url: str) -> str:
    """Derive the kebab-case repo name from a clone URL.

    Args:
        repo_url: The HTTPS clone URL (with or without a trailing .git).

    Returns:
        The repository basename, e.g. "mcp-gateway-registry".
    """
    return repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _clone_repo(task: Task, ref: str, clone_dir: str) -> Path:
    """Clone a task's repo at a ref into a temp dir named after the repo.

    The checkout lands at ``<clone_dir>/<mktemp>/<repo-name>`` so the /swe skill,
    which derives {repo-name} from the clone path's basename, gets the right name.

    Args:
        task: The task whose repo to clone.
        ref: The git ref (tag/branch/commit) to check out.
        clone_dir: Parent directory for the temporary clone.

    Returns:
        Path to the cloned repository.

    Raises:
        RuntimeError: If the clone command fails or times out.
    """
    name = _repo_name(task.repo)
    parent = Path(tempfile.mkdtemp(prefix="swe-", dir=clone_dir))
    dest = parent / name
    logger.info("  Cloning %s @ %s into %s", task.repo, ref, dest)
    try:
        subprocess.run(  # nosec B603 B607 - hardcoded git, args are dataset values, no shell
            [
                "git",
                "clone",
                "--branch",
                ref,
                "--depth",
                "1",
                task.repo,
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(f"git clone timed out for {task.repo} @ {ref}") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(
            f"git clone failed for {task.repo} @ {ref}: {exc.stderr.strip()[:500]}"
        ) from exc
    return dest


def _build_prompt(task: Task, clone_path: Path, ref: str, model: str) -> str:
    """Build the non-interactive /swe prompt for a task.

    Includes the four keys the skill needs to enter non-interactive mode
    (repo, problem, model, answers) plus the full problem statement and, when
    present, the reference issue URL.

    Args:
        task: The task to run.
        clone_path: Local path to the cloned repo.
        ref: The git ref checked out.
        model: The model name (also the artifact subfolder name).

    Returns:
        The prompt string to pass to `claude -p`.
    """
    answers = task.clarifying_answers or (
        "No separate answers provided. Use your best judgment; all needed "
        "information is in the task description below."
    )
    lines = [
        f"/swe repo: {clone_path} problem: {task.id} model: {model} "
        f'tag: {ref} answers: "{answers.strip()}"',
        "",
        "Task description:",
        task.problem_statement or "(see reference issue)",
    ]
    if task.problem_issue_url:
        lines += ["", f"Reference issue: {task.problem_issue_url}"]
    return "\n".join(lines)


def _build_env(config: RunnerConfig) -> dict[str, str]:
    """Build the environment for the claude subprocess from the runner config.

    Args:
        config: The runner config.

    Returns:
        A copy of the current environment with routing overrides applied.
    """
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = config.endpoint
    env["ANTHROPIC_API_KEY"] = config.api_key
    env["CLAUDE_CODE_USE_BEDROCK"] = "0"
    env["DISABLE_NON_ESSENTIAL_MODEL_CALLS"] = "1"
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(config.max_output_tokens)
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = config.model
    return env


def _build_settings_arg(config: RunnerConfig) -> str:
    """Build the value for `claude --settings`.

    A settings file's ``env`` block takes precedence over process environment
    variables, so relying on _build_env alone is not enough: a user's global
    ``~/.claude/settings.json`` (e.g. one that pins CLAUDE_CODE_USE_BEDROCK=1)
    would override our routing and the request would hit Bedrock, which rejects
    the local model id with a 400. Passing --settings overrides that global
    file, so we always supply one.

    Uses the configured ``settings_file`` when set; otherwise synthesizes an
    inline JSON settings object that pins routing at the config's endpoint.

    Args:
        config: The runner config.

    Returns:
        Either a settings file path or an inline JSON settings string.
    """
    if config.settings_file:
        return str(REPO_ROOT / config.settings_file)
    settings = {
        # Claude Code requires a token source even against a local endpoint that
        # ignores the value; without it the run fails with "Not logged in".
        "apiKeyHelper": f"echo {config.api_key}",
        "env": {
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "ANTHROPIC_BASE_URL": config.endpoint,
            "ANTHROPIC_API_KEY": config.api_key,
            "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(config.max_output_tokens),
            "CLAUDE_CODE_SUBAGENT_MODEL": config.model,
        },
    }
    return json.dumps(settings)


def _build_claude_cmd(config: RunnerConfig, prompt: str) -> list[str]:
    """Assemble the `claude -p` argument vector from the runner config.

    Args:
        config: The runner config.
        prompt: The /swe prompt to run.

    Returns:
        The command as a list of arguments (never a shell string).
    """
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        config.model,
        "--output-format",
        "json",
        "--permission-mode",
        config.permission_mode,
        "--allowedTools",
        ",".join(config.allowed_tools),
        "--max-turns",
        str(config.max_turns),
        "--settings",
        _build_settings_arg(config),
    ]


def _metrics_from_result(result: dict[str, Any], elapsed: float) -> dict[str, Any]:
    """Extract the six benchmark metrics from a claude -p JSON result.

    Args:
        result: The parsed JSON result object from `claude -p`.
        elapsed: Wall-clock seconds measured around the subprocess call.

    Returns:
        A metrics dictionary keyed by the dataset's metric names.
    """
    usage = result.get("usage") or {}
    duration_ms = result.get("duration_ms")
    latency = round(duration_ms / 1000, 1) if duration_ms else round(elapsed, 1)
    is_error = result.get("is_error", False)
    metrics = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "latency_seconds": latency,
        "num_turns": result.get("num_turns", 0),
        "total_cost_usd": result.get("total_cost_usd"),
        "is_error": is_error,
        "session_id": result.get("session_id"),
    }
    # Capture the error message so failures are diagnosable from metrics.json
    # without re-running the task by hand.
    if is_error:
        metrics["error"] = str(result.get("result", ""))[:1000]
        metrics["api_error_status"] = result.get("api_error_status")
    return metrics


def _run_claude(cmd: list[str], env: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run `claude -p` and parse its JSON result.

    Args:
        cmd: The command argument vector.
        env: Environment for the subprocess.
        timeout: Wall-clock timeout in seconds.

    Returns:
        The parsed JSON result object.

    Raises:
        RuntimeError: If claude times out, exits nonzero, or emits no JSON.
    """
    start = time.time()
    try:
        proc = subprocess.run(  # nosec B603 - hardcoded 'claude', list args, no shell
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude -p timed out after {timeout}s") from exc
    elapsed = time.time() - start

    if not proc.stdout.strip():
        raise RuntimeError(
            f"claude -p produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude -p output was not JSON: {proc.stdout.strip()[:500]}"
        ) from exc
    result["_elapsed_seconds"] = round(elapsed, 1)
    return result


def _artifact_dir(config: RunnerConfig, task: Task) -> Path:
    """Return the directory where /swe writes a task's artifacts.

    Mirrors the skill's convention:
    ``benchmarks/<output_dir>/<repo-name>/<task-id>/<model>/``.

    Args:
        config: The runner config.
        task: The task being run.

    Returns:
        The absolute artifact directory path.
    """
    return (
        REPO_ROOT
        / "benchmarks"
        / config.output_dir
        / _repo_name(task.repo)
        / task.id
        / config.model
    )


def _save_metrics(
    config: RunnerConfig, task: Task, ref: str, metrics: dict[str, Any]
) -> Path:
    """Write the run metrics to metrics.json in the artifact directory.

    Args:
        config: The runner config.
        task: The task that was run.
        ref: The git ref used.
        metrics: The metrics dictionary from _metrics_from_result.

    Returns:
        Path to the written metrics.json.
    """
    out_dir = _artifact_dir(config, task)
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = [f for f in ARTIFACT_FILENAMES if (out_dir / f).exists()]
    latency = metrics["latency_seconds"] or 0
    record = {
        "task": task.id,
        "repo": task.repo,
        "ref": ref,
        "complexity": task.complexity,
        "tags": task.tags,
        "model": config.model,
        "endpoint": config.endpoint,
        "artifacts_produced": len(produced),
        "artifacts_expected": len(ARTIFACT_FILENAMES),
        "generation_tokens_per_sec": (
            round(metrics["output_tokens"] / latency, 1) if latency > 0 else 0
        ),
        **metrics,
    }
    path = out_dir / "metrics.json"
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _run_task(config: RunnerConfig, dataset: Dataset, task: Task) -> dict[str, Any]:
    """Run a single task end to end and return its outcome summary.

    Args:
        config: The runner config.
        dataset: The loaded dataset (for default-ref resolution).
        task: The task to run.

    Returns:
        A summary dict: task id, ok flag, artifacts produced, and metrics.
    """
    ref = dataset.resolved_ref(task)
    logger.info("=== Task: %s [%s] ref=%s ===", task.id, task.complexity, ref)

    clone_path = _clone_repo(task, ref, config.clone_dir)
    clone_parent = clone_path.parent
    try:
        prompt = _build_prompt(task, clone_path, ref, config.model)
        cmd = _build_claude_cmd(config, prompt)
        env = _build_env(config)
        logger.info("  Running claude -p (max_turns=%s)...", config.max_turns)
        result = _run_claude(cmd, env, config.timeout_seconds)
        metrics = _metrics_from_result(result, result.get("_elapsed_seconds", 0))
    finally:
        shutil.rmtree(clone_parent, ignore_errors=True)

    metrics_path = _save_metrics(config, task, ref, metrics)
    out_dir = metrics_path.parent
    produced = [f for f in ARTIFACT_FILENAMES if (out_dir / f).exists()]
    ok = len(produced) == len(ARTIFACT_FILENAMES) and not metrics["is_error"]

    logger.info(
        "  %s: %s/%s artifacts, %s turns, %s in / %s out tokens, %ss",
        "OK" if ok else "INCOMPLETE",
        len(produced),
        len(ARTIFACT_FILENAMES),
        metrics["num_turns"],
        f"{metrics['input_tokens']:,}",
        f"{metrics['output_tokens']:,}",
        metrics["latency_seconds"],
    )
    if metrics["is_error"]:
        logger.error(
            "  claude -p reported an error (status %s): %s",
            metrics.get("api_error_status"),
            metrics.get("error"),
        )
    logger.info("  Metrics: %s", metrics_path)
    return {"task": task.id, "ok": ok, "artifacts": len(produced), "metrics": metrics}


def _select_tasks(dataset: Dataset, task_ids: list[str]) -> list[Task]:
    """Select tasks to run, preserving dataset order.

    Args:
        dataset: The loaded dataset.
        task_ids: Task ids to run; empty means all tasks.

    Returns:
        The tasks to run.

    Raises:
        DatasetError: If a requested id is not in the dataset.
    """
    if not task_ids:
        return dataset.tasks
    known = {t.id for t in dataset.tasks}
    missing = [tid for tid in task_ids if tid not in known]
    if missing:
        raise DatasetError(f"Unknown task ids: {missing}. Available: {sorted(known)}")
    return [t for t in dataset.tasks if t.id in set(task_ids)]


def _dry_run(config: RunnerConfig, dataset: Dataset, tasks: list[Task]) -> None:
    """Print the prompt and command for each task without executing anything."""
    for task in tasks:
        ref = dataset.resolved_ref(task)
        placeholder = Path(config.clone_dir) / "<tmp>" / _repo_name(task.repo)
        prompt = _build_prompt(task, placeholder, ref, config.model)
        cmd = _build_claude_cmd(config, prompt)
        print(f"\n=== {task.id} [{task.complexity}] ref={ref} ===")
        print("PROMPT:")
        print(prompt)
        print("\nCOMMAND:")
        print(" ".join(cmd))


def _run(config: RunnerConfig, dataset: Dataset, tasks: list[Task]) -> None:
    """Run every selected task and log a final pass/fail summary."""
    logger.info(
        "Running %s task(s) with model=%s against %s",
        len(tasks),
        config.model,
        config.endpoint,
    )
    summaries = []
    for task in tasks:
        try:
            summaries.append(_run_task(config, dataset, task))
        except RuntimeError:
            logger.exception("Task %s failed", task.id)
            summaries.append({"task": task.id, "ok": False, "artifacts": 0})

    passed = sum(1 for s in summaries if s["ok"])
    logger.info("=" * 60)
    logger.info("Done: %s/%s tasks produced all artifacts.", passed, len(summaries))
    for s in summaries:
        logger.info("  %s %s (%s artifacts)", "OK " if s["ok"] else "FAIL", s["task"], s["artifacts"])


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    CLI flags override the corresponding runner-config fields.
    """
    parser = argparse.ArgumentParser(
        description="Run the SWE benchmark headless via claude -p and the /swe skill.",
        epilog=(
            "Examples:\n"
            "  uv run scripts/run-swe-headless.py --config config/runner.example.yaml\n"
            "  uv run scripts/run-swe-headless.py --config config/runner.example.yaml "
            "--model qwen3-coder-30b --tasks remove-faiss,remove-efs-from-terraform-aws-ecs\n"
            "  uv run scripts/run-swe-headless.py --config config/runner.example.yaml --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="Path to the runner config YAML file")
    parser.add_argument("--endpoint", help="Override: API endpoint base URL")
    parser.add_argument("--model", help="Override: model name")
    parser.add_argument("--dataset", help="Override: dataset YAML path")
    parser.add_argument(
        "--tasks", help="Override: comma-separated task ids to run (default: all)"
    )
    parser.add_argument(
        "--max-turns", type=int, help="Override: cap on the agent loop"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print prompts/commands without running"
    )
    return parser.parse_args()


def main() -> None:
    """Parse arguments, load config and dataset, and run the benchmark."""
    args = _parse_args()
    overrides: dict[str, Any] = {
        "endpoint": args.endpoint,
        "model": args.model,
        "dataset": args.dataset,
        "max_turns": args.max_turns,
    }
    if args.tasks:
        overrides["tasks"] = [t.strip() for t in args.tasks.split(",") if t.strip()]

    try:
        config = load_runner_config(args.config, overrides)
    except RunnerConfigError as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    dataset_path = config.dataset
    if not Path(dataset_path).is_absolute():
        dataset_path = str(Path(__file__).resolve().parent.parent / dataset_path)
    try:
        dataset = load_dataset(dataset_path)
        tasks = _select_tasks(dataset, config.tasks)
    except DatasetError as exc:
        logger.error("Dataset error: %s", exc)
        sys.exit(1)

    if args.dry_run:
        _dry_run(config, dataset, tasks)
        return
    _run(config, dataset, tasks)


if __name__ == "__main__":
    main()
