"""Tests for the headless SWE harness helper functions.

These cover the pure, side-effect-free helpers (repo-name derivation, prompt
construction, metric extraction, artifact-path resolution). The subprocess and
git-clone paths are not exercised here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# The harness filename uses hyphens, so import it by path rather than name.
_HARNESS_PATH = _SCRIPTS_DIR / "run-swe-headless.py"
_spec = importlib.util.spec_from_file_location("run_swe_headless", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

import unittest  # noqa: E402

from dataset_loader import Task  # noqa: E402
from runner_config import RunnerConfig  # noqa: E402


def _task(**overrides: object) -> Task:
    """Build a Task with sensible defaults for testing."""
    data: dict[str, object] = {
        "id": "remove-faiss",
        "repo": "https://github.com/agentic-community/mcp-gateway-registry",
        "complexity": "medium",
        "tags": ["python"],
        "problem_statement": "Remove FAISS from the codebase.",
    }
    data.update(overrides)
    return Task.model_validate(data)


def _config(**overrides: object) -> RunnerConfig:
    """Build a RunnerConfig with sensible defaults for testing."""
    data: dict[str, object] = {
        "endpoint": "http://127.0.0.1:8000",
        "model": "qwen3.6-35b",
        "dataset": "dataset/example.yaml",
    }
    data.update(overrides)
    return RunnerConfig.model_validate(data)


class RepoNameTest(unittest.TestCase):
    def test_derives_basename(self) -> None:
        self.assertEqual(
            harness._repo_name("https://github.com/foo/mcp-gateway-registry"),
            "mcp-gateway-registry",
        )

    def test_strips_git_suffix_and_trailing_slash(self) -> None:
        self.assertEqual(
            harness._repo_name("https://github.com/foo/bar.git/"), "bar"
        )


class BuildPromptTest(unittest.TestCase):
    def test_prompt_has_all_swe_keys(self) -> None:
        prompt = harness._build_prompt(
            _task(), Path("/tmp/x/mcp-gateway-registry"), "1.24.4", "qwen3.6-35b"
        )
        for key in ("repo:", "problem:", "model:", "answers:"):
            self.assertIn(key, prompt)
        self.assertIn("remove-faiss", prompt)

    def test_prompt_includes_issue_url_when_present(self) -> None:
        prompt = harness._build_prompt(
            _task(problem_issue_url="https://github.com/foo/bar/issues/1"),
            Path("/tmp/x/bar"),
            "main",
            "m",
        )
        self.assertIn("Reference issue:", prompt)

    def test_prompt_has_fallback_answers_when_absent(self) -> None:
        prompt = harness._build_prompt(
            _task(clarifying_answers=None), Path("/tmp/x/r"), "main", "m"
        )
        self.assertIn("best judgment", prompt)


class MetricsFromResultTest(unittest.TestCase):
    def test_extracts_six_metrics(self) -> None:
        result = {
            "num_turns": 12,
            "duration_ms": 45000,
            "total_cost_usd": 0.12,
            "is_error": False,
            "session_id": "abc",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 50,
            },
        }
        metrics = harness._metrics_from_result(result, elapsed=99.0)
        self.assertEqual(metrics["input_tokens"], 1000)
        self.assertEqual(metrics["output_tokens"], 500)
        self.assertEqual(metrics["cache_read_tokens"], 200)
        self.assertEqual(metrics["cache_creation_tokens"], 50)
        self.assertEqual(metrics["num_turns"], 12)
        # duration_ms wins over the measured elapsed time.
        self.assertEqual(metrics["latency_seconds"], 45.0)

    def test_falls_back_to_elapsed_without_duration(self) -> None:
        metrics = harness._metrics_from_result({"usage": {}}, elapsed=7.25)
        self.assertEqual(metrics["latency_seconds"], 7.2)
        self.assertEqual(metrics["num_turns"], 0)


class ArtifactDirTest(unittest.TestCase):
    def test_path_follows_skill_convention(self) -> None:
        path = harness._artifact_dir(_config(output_dir="swe-benchmark-data"), _task())
        self.assertEqual(path.parts[-4:], (
            "swe-benchmark-data",
            "mcp-gateway-registry",
            "remove-faiss",
            "qwen3.6-35b",
        ))


class BuildClaudeCmdTest(unittest.TestCase):
    def test_never_uses_bypass_permissions(self) -> None:
        cmd = harness._build_claude_cmd(_config(), "prompt")
        joined = " ".join(cmd)
        self.assertNotIn("bypassPermissions", joined)
        self.assertNotIn("dangerously-skip-permissions", joined)
        self.assertIn("acceptEdits", cmd)

    def test_includes_json_output_and_max_turns(self) -> None:
        cmd = harness._build_claude_cmd(_config(max_turns=42), "prompt")
        self.assertIn("json", cmd)
        self.assertIn("42", cmd)

    def test_always_passes_settings(self) -> None:
        # --settings must always be present so it overrides a user's global
        # ~/.claude/settings.json (e.g. one that pins Bedrock routing).
        cmd = harness._build_claude_cmd(_config(), "prompt")
        self.assertIn("--settings", cmd)


class BuildSettingsArgTest(unittest.TestCase):
    def test_inline_json_pins_routing_when_no_file(self) -> None:
        import json

        arg = harness._build_settings_arg(_config(endpoint="http://127.0.0.1:8000"))
        settings = json.loads(arg)
        self.assertEqual(settings["env"]["CLAUDE_CODE_USE_BEDROCK"], "0")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8000")

    def test_uses_settings_file_when_configured(self) -> None:
        arg = harness._build_settings_arg(
            _config(settings_file="self-hosted/vllm/config/claude-code.json")
        )
        self.assertTrue(arg.endswith("self-hosted/vllm/config/claude-code.json"))


class MetricsErrorTest(unittest.TestCase):
    def test_captures_error_message_on_failure(self) -> None:
        result = {
            "is_error": True,
            "api_error_status": 400,
            "result": "API Error (qwen3.6-35b): 400 The provided model identifier is invalid..",
            "usage": {},
        }
        metrics = harness._metrics_from_result(result, elapsed=0.2)
        self.assertTrue(metrics["is_error"])
        self.assertEqual(metrics["api_error_status"], 400)
        self.assertIn("invalid", metrics["error"])

    def test_no_error_field_on_success(self) -> None:
        metrics = harness._metrics_from_result({"is_error": False, "usage": {}}, 1.0)
        self.assertNotIn("error", metrics)


if __name__ == "__main__":
    unittest.main()
