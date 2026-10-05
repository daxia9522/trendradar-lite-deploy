"""Actions AI env contracts: new ordered lists only, never business runs.

Run with LITELLM_LOCAL_MODEL_COST_MAP=True and installed project dependencies.
No GitHub access, real credentials, AI completion, storage writes or email sends.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True), \
        patch("socket.create_connection", side_effect=AssertionError("network during import")), \
        patch("socket.socket.connect", side_effect=AssertionError("network during import")), \
        patch("socket.socket.connect_ex", side_effect=AssertionError("network during import")), \
        patch("socket.getaddrinfo", side_effect=AssertionError("network during import")):
    from trendradar.ai.client import AIClient, build_keyword_client
    from trendradar.core.loader import load_ai_config, load_config


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ("crawler.yml", "crawl", "Run crawler", "python -m trendradar"),
    ("weekly-report.yml", "weekly-report", "Generate weekly report and send email",
     "python weekly_report/weekly_ai_report_email.py"),
)
SHARED_AI_KEYS = (
    "AI_MODEL", "AI_API_KEY", "AI_API_BASE", "AI_FALLBACK_MODELS",
    "AI_FALLBACK_API_BASE", "AI_FALLBACK_API_KEY",
    "AI_TIMEOUT",
)
FALLBACK_AUTH_KEYS = (
    "AI_FALLBACK_API_BASE", "AI_FALLBACK_API_KEY",
)
SECRET_REFERENCE = re.compile(r"\$\{\{ secrets\.([A-Z_]+) \}\}")
SYNTHETIC_SECRETS = {
    "AI_MODEL": "gemini/test-primary",
    "AI_API_KEY": "synthetic-gemini-key",
    "AI_API_BASE": "",
    "AI_FALLBACK_MODELS": "gemini/test-lite,openai/test-fallback",
    "AI_OPENAI_API_KEY": "synthetic-openai-key",
    "AI_OPENAI_API_BASE": "https://relay.example.invalid/v1",
    "AI_TIMEOUT": "120",
}
SYNTHETIC_LIST_SECRETS = {
    "AI_MODEL": "gemini/test-primary",
    "AI_API_KEY": "synthetic-gemini-key",
    "AI_API_BASE": "",
    "AI_FALLBACK_MODELS": "gemini/test-lite,openai/@cf/test-fallback",
    "AI_FALLBACK_API_BASE": "@https://relay.example.invalid/v1",
    "AI_FALLBACK_API_KEY": "@synthetic-relay-key",
    "AI_TIMEOUT": "120",
}


def read_workflow(filename):
    # BaseLoader preserves GitHub's `on` as a string instead of a YAML 1.1 bool.
    return yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)


def business_step(data, job_name, step_name):
    return next(step for step in data["jobs"][job_name]["steps"] if step.get("name") == step_name)


def synthetic_step_env(step, secrets):
    """Resolve ONLY exact same-name secret references; no Actions expression eval."""
    values = {}
    for key, reference in step["env"].items():
        if not key.startswith("AI_"):
            continue
        match = SECRET_REFERENCE.fullmatch(reference)
        if not match or match[1] != key:
            raise AssertionError(f"Expected same-name Secret reference for {key}")
        values[key] = secrets.get(match[1], "")
    return values


class ActionsAIEnvironmentTests(unittest.TestCase):
    def setUp(self):
        # Strip ambient credentials, including *_FILE/provider-specific variables.
        clean_env = patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True)
        clean_env.start()
        self.addCleanup(clean_env.stop)
        self.guards = []
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.getaddrinfo", "smtplib.SMTP", "smtplib.SMTP_SSL",
                       "trendradar.ai.client.completion"):
            guard = patch(target, side_effect=AssertionError("offline contract attempted external work"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.output = io.StringIO()
        output = redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def shared_configs(self, step, secrets):
        """Load daily and weekly entrypoints under only the synthetic step env."""
        with patch.dict(os.environ, synthetic_step_env(step, secrets)):
            daily = load_config(str(ROOT / "config/config.yaml"))["AI"]
            weekly = load_ai_config(str(ROOT / "config/config.yaml"))
            self.assertEqual(daily, weekly)
            return daily, (AIClient(daily), AIClient(weekly))

    def assert_candidates(self, client, expected):
        self.assertIsInstance(client.candidates, tuple)
        self.assertEqual(
            [(item.id, item.model, item.api_base or "", item.api_key) for item in client.candidates],
            expected,
        )
        for item in client.candidates:
            self.assertFalse(item.error)
        self.assertIsNone(client.last_candidate_id)

    def test_both_business_steps_inject_identical_same_name_ai_secrets(self):
        mappings = []
        for filename, job_name, step_name, command in WORKFLOWS:
            with self.subTest(workflow=filename):
                data = read_workflow(filename)
                step = business_step(data, job_name, step_name)
                self.assertEqual(step["run"], command)
                mapping = {key: step["env"].get(key) for key in SHARED_AI_KEYS}
                for key, value in mapping.items():
                    self.assertEqual(value, "${{ secrets." + key + " }}")
                mappings.append(mapping)
                self.assertEqual(set(data["on"]), {"workflow_dispatch"})
        self.assertEqual(*mappings)

    def test_new_credentials_are_scoped_to_business_step_not_setup_or_download(self):
        for filename, job_name, step_name, _ in WORKFLOWS:
            data = read_workflow(filename)
            for key in FALLBACK_AUTH_KEYS:
                with self.subTest(workflow=filename, key=key):
                    self.assertNotIn(key, data.get("env", {}))
                    for name, job in data["jobs"].items():
                        self.assertNotIn(key, job.get("env", {}))
                        for step in job["steps"]:
                            if name == job_name and step.get("name") == step_name:
                                continue
                            self.assertNotIn(key, step.get("env", {}))

    def test_step_env_preserves_leading_trailing_consecutive_and_whole_blank_columns(self):
        # This proves the Actions mapping itself never splits/trims/filters a Secret.
        examples = (
            ("@https://second.example.invalid/v1", "@synthetic-second-key"),
            ("https://first.example.invalid/v1@", "synthetic-first-key@"),
            ("@", "@"),
            ("https://first.example.invalid/v1@@", "synthetic-first-key@@"),
            ("", "@"),
            ("@", ""),
            ("   ", "@"),
        )
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            for bases, keys in examples:
                with self.subTest(workflow=filename, bases=bases):
                    secrets = dict(SYNTHETIC_LIST_SECRETS, AI_FALLBACK_API_BASE=bases,
                                   AI_FALLBACK_API_KEY=keys)
                    values = synthetic_step_env(step, secrets)
                    self.assertEqual(values["AI_FALLBACK_API_BASE"], bases)
                    self.assertEqual(values["AI_FALLBACK_API_KEY"], keys)
                    self.assertNotIn("AI_OPENAI_API_KEY", values)
                    self.assertNotIn("AI_OPENAI_API_BASE", values)

    def test_retired_secrets_are_not_injected_or_merged(self):
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            self.assertFalse(any(key.startswith("AI_OPENAI_") for key in step["env"]))
            config, clients = self.shared_configs(step, dict(SYNTHETIC_LIST_SECRETS, AI_OPENAI_API_KEY="synthetic-ignored"))
            self.assertFalse(any("OPENAI" in key for key in config))
            for client in clients:
                self.assertEqual([c.id for c in client.candidates], ["main", "fallback:1", "fallback:2"])

    def test_list_values_bind_gemini_then_cf_for_daily_weekly_and_keywords(self):
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            for legacy_blank in ("", "   "):
                with self.subTest(workflow=filename, legacy_blank=legacy_blank):
                    secrets = dict(SYNTHETIC_LIST_SECRETS, AI_OPENAI_API_KEY=legacy_blank,
                                   AI_OPENAI_API_BASE=legacy_blank)
                    config, clients = self.shared_configs(step, secrets)
                    self.assertEqual(config["FALLBACK_API_BASE"], secrets["AI_FALLBACK_API_BASE"])
                    self.assertEqual(config["FALLBACK_API_KEY"], secrets["AI_FALLBACK_API_KEY"])
                    self.assertEqual(config["FALLBACK_MODELS"],
                                     ["gemini/test-lite", "openai/@cf/test-fallback"])
                    expected = [
                        ("main", "gemini/test-primary", "", "synthetic-gemini-key"),
                        ("fallback:1", "gemini/test-lite", "", "synthetic-gemini-key"),
                        ("fallback:2", "openai/@cf/test-fallback",
                         "https://relay.example.invalid/v1", "synthetic-relay-key"),
                    ]
                    for client in clients:
                        self.assert_candidates(client, expected)
                        self.assertEqual(client.timeout, 120)
                        keyword = build_keyword_client(client)
                        self.assert_candidates(keyword, [expected[1]])
                        self.assertEqual(keyword.timeout, 120)
                        self.assertEqual(len(client.candidates), 3)

    def test_new_columns_align_with_yaml_models_when_model_secret_is_blank(self):
        config_data = {"ai": {
            "model": "gemini/yaml-primary", "api_key": "synthetic-yaml-primary-key",
            "fallback_models": ["gemini/yaml-lite", "openai/yaml-fallback"], "timeout": 64,
        }}
        expected = [
            ("main", "gemini/yaml-primary", "", "synthetic-yaml-primary-key"),
            ("fallback:1", "gemini/yaml-lite", "", "synthetic-yaml-primary-key"),
            ("fallback:2", "openai/yaml-fallback", "https://relay.example.invalid/v1", "synthetic-relay-key"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(config_data))
            for filename, job_name, step_name, _ in WORKFLOWS:
                step = business_step(read_workflow(filename), job_name, step_name)
                for models in ("", "   "):
                    with self.subTest(workflow=filename, model_secret=models):
                        secrets = {
                            "AI_FALLBACK_MODELS": models,
                            "AI_FALLBACK_API_BASE": "@https://relay.example.invalid/v1",
                            "AI_FALLBACK_API_KEY": "@synthetic-relay-key",
                        }
                        with patch.dict(os.environ, synthetic_step_env(step, secrets)):
                            daily = load_config(str(path))["AI"]
                            weekly = load_ai_config(str(path))
                            self.assertEqual(daily, weekly)
                            for config in (daily, weekly):
                                client = AIClient(config)
                                self.assert_candidates(client, expected)
                                self.assertEqual(client.timeout, 64)

    def test_openai_list_keeps_same_model_at_three_independent_endpoints(self):
        secrets = {
            "AI_MODEL": "openai/test-shared-model",
            "AI_API_KEY": "synthetic-primary-key",
            "AI_API_BASE": "https://primary.example.invalid/v1",
            "AI_FALLBACK_MODELS": "openai/test-shared-model,openai/test-shared-model",
            "AI_FALLBACK_API_BASE": "https://first.example.invalid/v1@https://second.example.invalid/v1",
            "AI_FALLBACK_API_KEY": "synthetic-first-key@synthetic-second-key",
            "AI_TIMEOUT": "37",
        }
        expected = [
            ("main", "openai/test-shared-model", "https://primary.example.invalid/v1", "synthetic-primary-key"),
            ("fallback:1", "openai/test-shared-model", "https://first.example.invalid/v1", "synthetic-first-key"),
            ("fallback:2", "openai/test-shared-model", "https://second.example.invalid/v1", "synthetic-second-key"),
        ]
        for filename, job_name, step_name, _ in WORKFLOWS:
            with self.subTest(workflow=filename):
                step = business_step(read_workflow(filename), job_name, step_name)
                config, clients = self.shared_configs(step, secrets)
                self.assertEqual(config["FALLBACK_MODELS"],
                                 ["openai/test-shared-model", "openai/test-shared-model"])
                for client in clients:
                    self.assert_candidates(client, expected)
                    self.assertEqual(client.timeout, 37)
                    keyword = build_keyword_client(config)
                    self.assert_candidates(keyword, [expected[1]])
                    self.assertEqual(keyword.timeout, 37)

    def test_new_list_empty_slots_survive_loading_and_whole_blank_column_expands(self):
        first = "https://first.example.invalid/v1"
        second = "https://second.example.invalid/v1"
        primary_key = SYNTHETIC_LIST_SECRETS["AI_API_KEY"]
        cases = (
            (f"@{second}", "@synthetic-second-key", ("", second), (primary_key, "synthetic-second-key")),
            (f"{first}@", "synthetic-first-key@", (first, ""), ("synthetic-first-key", primary_key)),
            ("@", "@", ("", ""), (primary_key, primary_key)),
            ("@", "", ("", ""), (primary_key, primary_key)),
            ("", "@", ("", ""), (primary_key, primary_key)),
            ("   ", "@", ("", ""), (primary_key, primary_key)),
            ("", "synthetic-first-key@synthetic-second-key", ("", ""),
             ("synthetic-first-key", "synthetic-second-key")),
            (f"{first}@@{second}", "synthetic-first-key@@synthetic-second-key",
             (first, "", second), ("synthetic-first-key", primary_key, "synthetic-second-key")),
        )
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            for bases, keys, expected_bases, expected_keys in cases:
                with self.subTest(workflow=filename, bases=bases, count=len(expected_bases)):
                    models = [f"gemini/test-lite-{index}" for index in range(len(expected_bases))]
                    secrets = dict(SYNTHETIC_LIST_SECRETS, AI_FALLBACK_MODELS=",".join(models),
                                   AI_FALLBACK_API_BASE=bases, AI_FALLBACK_API_KEY=keys)
                    config, clients = self.shared_configs(step, secrets)
                    self.assertEqual(config["FALLBACK_API_BASE"], bases.strip())
                    self.assertEqual(config["FALLBACK_API_KEY"], keys)
                    expected = [("main", "gemini/test-primary", "", primary_key)]
                    expected.extend(
                        (f"fallback:{index + 1}", model, base, key)
                        for index, (model, base, key) in enumerate(zip(models, expected_bases, expected_keys))
                    )
                    for client in clients:
                        self.assert_candidates(client, expected)

    def test_blank_key_column_never_inherits_to_another_endpoint(self):
        secrets = dict(SYNTHETIC_LIST_SECRETS,
                       AI_MODEL="openai/test-primary", AI_API_BASE="https://primary.example.invalid/v1",
                       AI_FALLBACK_MODELS="openai/test-first,openai/test-second",
                       AI_FALLBACK_API_BASE="https://first.example.invalid/v1@https://second.example.invalid/v1",
                       AI_FALLBACK_API_KEY="")
        for filename, job_name, step_name, _ in WORKFLOWS:
            with self.subTest(workflow=filename):
                step = business_step(read_workflow(filename), job_name, step_name)
                _, clients = self.shared_configs(step, secrets)
                for client in clients:
                    self.assertFalse(client.candidates[0].error)
                    for candidate in client.candidates[1:]:
                        self.assertFalse(candidate.api_key)
                        self.assertTrue(candidate.error)
                        self.assertNotIn(secrets["AI_API_KEY"], candidate.error)

    def test_at_only_secret_activates_new_mode_not_primary_relay_inheritance(self):
        # In legacy mode these same-provider models share the custom primary base.
        # A literal @ must activate list mode: their empty slots mean official defaults.
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            for bases, keys in (("@", ""), ("", "@"), ("@", "   "), ("   ", "@")):
                with self.subTest(workflow=filename, bases=bases, keys=keys):
                    secrets = dict(
                        SYNTHETIC_LIST_SECRETS,
                        AI_API_BASE="https://primary.example.invalid/v1",
                        AI_FALLBACK_MODELS="gemini/test-first,gemini/test-second",
                        AI_FALLBACK_API_BASE=bases, AI_FALLBACK_API_KEY=keys,
                    )
                    _, clients = self.shared_configs(step, secrets)
                    for client in clients:
                        self.assertEqual(len(client.candidates), 3)
                        self.assertFalse(client.candidates[0].error)
                        for candidate in client.candidates[1:]:
                            self.assertFalse(candidate.api_base)
                            self.assertFalse(candidate.api_key)
                            self.assertTrue(candidate.error)
                        keyword = build_keyword_client(client)
                        self.assertEqual(len(keyword.candidates), 1)
                        self.assertEqual(keyword.candidates[0].id, "fallback:1")
                        self.assertTrue(keyword.candidates[0].error)

    def test_invalid_list_counts_empty_models_and_mixed_modes_fail_closed(self):
        cases = (
            {"AI_FALLBACK_API_BASE": "https://one.example.invalid/v1"},
            {"AI_FALLBACK_API_BASE": "@@"},
            {"AI_FALLBACK_API_KEY": "synthetic-one-key"},
            {"AI_FALLBACK_MODELS": ""},
            {"AI_FALLBACK_MODELS": "gemini/test-lite,,openai/test-third"},
            {"AI_FALLBACK_MODELS": ",gemini/test-lite"},
            {"AI_FALLBACK_MODELS": "gemini/test-lite,"},
            {"AI_FALLBACK_MODELS": "missing-provider,gemini/test-lite"},
        )
        for filename, job_name, step_name, _ in WORKFLOWS:
            step = business_step(read_workflow(filename), job_name, step_name)
            for index, overrides in enumerate(cases):
                secrets = dict(SYNTHETIC_LIST_SECRETS, **overrides)
                with self.subTest(workflow=filename, case=index):
                    with patch.dict(os.environ, synthetic_step_env(step, secrets)):
                        for load in (load_ai_config, lambda path: load_config(path)["AI"]):
                            try:
                                client = AIClient(load(str(ROOT / "config/config.yaml")))
                                valid, error = client.validate_config()
                            except ValueError as exc:
                                valid, error = False, str(exc)
                            self.assertFalse(valid, "ambiguous Actions bindings must not be accepted")
                            self.assertTrue(error)
                            for key, value in secrets.items():
                                if key.endswith("_KEY") and value:
                                    self.assertNotIn(value, error)
                                    self.assertNotIn(value, self.output.getvalue())

    def test_absent_or_blank_secrets_keep_yaml_and_leave_second_auth_disabled(self):
        # Includes an explicit non-default timeout to prove fallback, not hardcoding.
        config_data = {"ai": {"model": "gemini/yaml-primary", "api_key": "synthetic-yaml-key",
                              "fallback_models": ["gemini/yaml-lite"], "timeout": 64}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(config_data))
            for filename, job_name, step_name, _ in WORKFLOWS:
                step = business_step(read_workflow(filename), job_name, step_name)
                for secrets in ({}, {key: "" for key in SHARED_AI_KEYS},
                                {key: "   " for key in SHARED_AI_KEYS}):
                    with self.subTest(workflow=filename, absent=not secrets):
                        with patch.dict(os.environ, synthetic_step_env(step, secrets)):
                            config = load_ai_config(str(path))
                            self.assertEqual(config["MODEL"], "gemini/yaml-primary")
                            self.assertEqual(config["FALLBACK_MODELS"], ["gemini/yaml-lite"])
                            self.assertEqual(config["TIMEOUT"], 64)
                            self.assertFalse(any("OPENAI" in key for key in config))
                            self.assertEqual(AIClient(config).validate_config(), (True, ""))

    def test_examples_keep_keys_blank_and_document_shared_secret_contract(self):
        example = {}
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                example[key] = value
        for key in SHARED_AI_KEYS:
            self.assertIn(key, example)
        for key in ("AI_API_KEY", *FALLBACK_AUTH_KEYS):
            self.assertEqual(example[key], "")
        for key in ("AI_API_KEY_FILE",):
            self.assertIn(key, example)
            self.assertEqual(example[key], "")
        self.assertNotIn("AI_FALLBACK_API_KEY_FILE", example)
        self.assertEqual(example["AI_TIMEOUT"], "120")
        config = yaml.safe_load((ROOT / "config/config.yaml").read_text())
        self.assertEqual(config["ai"]["timeout"], 120)
        self.assertFalse(config["ai"].get("api_key"))
        self.assertFalse(config["ai"].get("openai_api_key"))
        self.assertFalse(config["ai"].get("fallback_api_key"))
        readme = (ROOT / "README.md").read_text()
        for key in SHARED_AI_KEYS:
            self.assertIn(f"`{key}`", readme)
        self.assertIn("同名 Actions Secrets", readme)


if __name__ == "__main__":
    unittest.main()
