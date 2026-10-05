"""Offline candidate-list contracts: positional binding, routing and secret isolation."""
import ast
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_ai_auth_config import OfflineAITestCase, SyntheticHTTPError, PRIMARY_KEY
from trendradar.ai.client import AIClient, build_keyword_client
from trendradar.ai_config import build_candidates, validate_fallback_settings
from trendradar.core import loader

BASE1 = "https://primary.example.invalid/v1"
BASE2 = "https://secondary.example.invalid/v1"
BASE3 = "https://third.example.invalid/v1"
KEY2 = "synthetic-secondary-key"
KEY3 = "synthetic-third-key"


def config(**overrides):
    value = {
        "MODEL": "gemini/main", "API_KEY": PRIMARY_KEY, "API_BASE": "",
        "FALLBACK_MODELS": ["gemini/lite", "openai/@cf/qwen/qwen3.8-27b"],
        "FALLBACK_API_BASE": "@" + BASE3, "FALLBACK_API_KEY": "@" + KEY3,
    }
    value.update(overrides)
    return value


class CandidateConfigurationTests(unittest.TestCase):
    def test_requested_example_binds_empty_first_slots_and_explicit_second_pair(self):
        result = build_candidates(config())
        self.assertFalse(result.error)
        primary, first, second = result.candidates
        self.assertEqual((first.id, first.api_base, first.api_key, first.inherits_primary),
                         ("fallback:1", "", PRIMARY_KEY, True))
        self.assertEqual((second.id, second.api_base, second.api_key), ("fallback:2", BASE3, KEY3))
        self.assertNotIn(PRIMARY_KEY, repr(result))
        self.assertNotIn(KEY3, repr(result))
        self.assertNotIn(BASE3, repr(result))

    def test_all_empty_columns_expand_only_in_explicit_list_mode(self):
        result = build_candidates(config(FALLBACK_MODELS=["gemini/a", "gemini/b"],
                                         FALLBACK_API_BASE="@", FALLBACK_API_KEY=""))
        self.assertEqual([c.api_key for c in result.candidates], [PRIMARY_KEY] * 3)
        self.assertFalse(any(c.error for c in result.candidates))
        for values in ({}, {"FALLBACK_API_BASE": " ", "FALLBACK_API_KEY": ""}):
            result = build_candidates({"MODEL": "gemini/main", "API_KEY": PRIMARY_KEY,
                                       "FALLBACK_MODELS": ["gemini/lite"], **values})
            self.assertFalse(result.error)

    def test_trailing_and_middle_empty_slots_retain_original_ids(self):
        result = build_candidates(config(FALLBACK_MODELS=["openai/a", "gemini/b", "gemini/c"],
                                         FALLBACK_API_BASE=BASE2 + "@@", FALLBACK_API_KEY=KEY2 + "@@"))
        self.assertEqual([c.api_base for c in result.candidates[1:]], [BASE2, "", ""])
        self.assertEqual([c.api_key for c in result.candidates[1:]], [KEY2, PRIMARY_KEY, PRIMARY_KEY])
        self.assertEqual([c.id for c in result.candidates[1:]], ["fallback:1", "fallback:2", "fallback:3"])

    def test_same_provider_different_base_cannot_inherit_primary_key(self):
        result = build_candidates(config(MODEL="openai/main", API_BASE=BASE1,
                                         FALLBACK_MODELS=["openai/lite"],
                                         FALLBACK_API_BASE=BASE2, FALLBACK_API_KEY=""))
        self.assertEqual(result.candidates[1].api_key, "")
        self.assertEqual(result.candidates[1].error_kind, "api_key")

    def test_empty_url_means_provider_default_not_primary_custom_base(self):
        result = build_candidates(config(API_BASE=BASE1, FALLBACK_API_KEY="@" + KEY3))
        self.assertEqual(result.candidates[1].api_base, "")
        self.assertEqual(result.candidates[1].error_kind, "api_key")

    def test_explicit_equal_endpoint_can_reuse_key_but_different_provider_cannot(self):
        result = build_candidates(config(API_BASE=BASE1, FALLBACK_API_BASE=BASE1 + "@" + BASE1,
                                         FALLBACK_API_KEY="@"))
        self.assertEqual(result.candidates[1].api_key, PRIMARY_KEY)
        self.assertEqual(result.candidates[2].error_kind, "api_key")

    def test_urls_are_compared_conservatively(self):
        for target, inherits in (("https://PRIMARY.example.invalid:443/v1", True),
                                  (BASE1 + "/", False), (BASE1 + "?q=1", False)):
            with self.subTest(target=target):
                result = build_candidates(config(API_BASE=BASE1, FALLBACK_MODELS=["gemini/a"],
                                                 FALLBACK_API_BASE=target, FALLBACK_API_KEY=""))
                self.assertEqual(result.candidates[1].inherits_primary, inherits)

    def test_structure_errors_are_value_free_and_never_zip_truncate(self):
        cases = [
            {"FALLBACK_API_BASE": BASE3},
            {"FALLBACK_API_KEY": KEY3},
            {"FALLBACK_API_BASE": "@" + BASE3 + "@"},
            {"FALLBACK_MODELS": []},
            {"FALLBACK_MODELS": "gemini/a,,openai/b"},
            {"FALLBACK_MODELS": "gemini/a, "},
            {"FALLBACK_MODELS": ["gemini/a", "openai/"]},
        ]
        for overrides in cases:
            with self.subTest(fields=list(overrides)):
                result = build_candidates(config(**overrides))
                self.assertTrue(result.error)
                self.assertEqual(len(result.candidates), 1)
                for private in (KEY2, KEY3, BASE2, BASE3, "/synthetic/private-key-file"):
                    self.assertNotIn(private, result.error)

    def test_bad_individual_url_disables_only_that_slot(self):
        for bad in ("not-a-url", "https://host.invalid:invalid/v1", "https://host.invalid/v1#fragment",
                    "https://host.invalid/path with space"):
            with self.subTest(bad=bad):
                result = build_candidates(config(FALLBACK_API_BASE=bad + "@" + BASE3,
                                                 FALLBACK_API_KEY=KEY2 + "@" + KEY3))
                self.assertFalse(result.error)
                self.assertEqual(result.candidates[1].error_kind, "api_base")
                self.assertFalse(result.candidates[2].error)
                self.assertNotIn(bad, result.candidates[1].error)

    def test_shared_menu_validation_ignores_optional_missing_key_but_checks_structure(self):
        values = {"AI_MODEL": "gemini/main", "AI_FALLBACK_MODELS": "openai/b",
                  "AI_FALLBACK_API_BASE": BASE2, "AI_FALLBACK_API_KEY": ""}
        self.assertEqual(validate_fallback_settings(values), [])
        self.assertTrue(validate_fallback_settings(dict(values, AI_FALLBACK_MODELS="openai/b,openai/c")))
        self.assertTrue(validate_fallback_settings(dict(values, AI_FALLBACK_API_BASE="not-a-url")))

    def test_helper_loads_standalone_without_importing_application_or_sdk(self):
        path = Path(__file__).resolve().parents[1] / "trendradar/ai_config.py"
        tree = ast.parse(path.read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name and (name.startswith("trendradar") or name.startswith("litellm")) for name in imports))
        name = "synthetic_standalone_ai_config"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
            self.assertFalse(module.build_candidates(config()).error)


class CandidateRoutingTests(OfflineAITestCase):
    def test_three_openai_endpoints_with_identical_models_are_all_attempted(self):
        client = AIClient(config(MODEL="openai/same", API_BASE=BASE1,
                                 FALLBACK_MODELS=["openai/same", "openai/same"],
                                 FALLBACK_API_BASE=BASE2 + "@" + BASE3,
                                 FALLBACK_API_KEY=KEY2 + "@" + KEY3))
        self.completion.side_effect = [SyntheticHTTPError(408), SyntheticHTTPError(408), self.response]
        self.assertEqual(client.chat(self.messages), "offline answer")
        calls = [entry.kwargs for entry in self.completion.call_args_list]
        self.assertEqual([p["model"] for p in calls], ["openai/same"] * 3)
        self.assertEqual([p["api_base"] for p in calls], [BASE1, BASE2, BASE3])
        self.assertEqual([p["api_key"] for p in calls], [PRIMARY_KEY, KEY2, KEY3])
        self.assertEqual(client.last_candidate_id, "fallback:2")
        self.sleep.assert_not_called()
        for private in (BASE1, BASE2, BASE3, PRIMARY_KEY, KEY2, KEY3):
            self.assertNotIn(private, self.output.getvalue())

    def test_same_provider_cross_endpoint_headers_do_not_leak(self):
        extra = {"headers": {"Authorization": PRIMARY_KEY}, "extra_headers": {"x-api-key": PRIMARY_KEY},
                 "provider_specific_header": {"x-private": PRIMARY_KEY}}
        self.completion.side_effect = [SyntheticHTTPError(408), self.response]
        client = AIClient(config(MODEL="openai/main", API_BASE=BASE1,
                                 FALLBACK_MODELS=["openai/lite"], FALLBACK_API_BASE=BASE2,
                                 FALLBACK_API_KEY=KEY2, EXTRA_PARAMS=extra))
        client.chat(self.messages)
        first, second = [entry.kwargs for entry in self.completion.call_args_list]
        self.assertIn("headers", first)
        for field in ("headers", "extra_headers", "provider_specific_header"):
            self.assertNotIn(field, second)
        self.assertEqual((second["api_key"], second["api_base"]), (KEY2, BASE2))
        self.assertNotIn(PRIMARY_KEY, repr(second))
        self.assertEqual(extra["headers"]["Authorization"], PRIMARY_KEY)

    def test_new_candidate_key_on_same_endpoint_does_not_reuse_old_headers(self):
        self.completion.side_effect = [SyntheticHTTPError(408), self.response]
        client = AIClient(config(MODEL="openai/main", API_BASE=BASE1,
                                 FALLBACK_MODELS=["openai/lite"], FALLBACK_API_BASE=BASE1,
                                 FALLBACK_API_KEY=KEY2, EXTRA_PARAMS={"headers": {"Authorization": PRIMARY_KEY}}))
        client.chat(self.messages)
        self.assertNotIn("headers", self.completion.call_args.kwargs)

    def test_keyword_binds_first_slot_and_rederivation_never_changes_it(self):
        parent = AIClient(config(MODEL="openai/main", API_BASE=BASE1,
                                 FALLBACK_MODELS=["openai/same", "openai/same"],
                                 FALLBACK_API_BASE=BASE2 + "@" + BASE3,
                                 FALLBACK_API_KEY=KEY2 + "@" + KEY3))
        parent.last_candidate_id = "fallback:2"
        keyword = build_keyword_client(build_keyword_client(parent))
        keyword.chat(self.messages)
        self.assertEqual((self.completion.call_args.kwargs["api_base"], self.completion.call_args.kwargs["api_key"]),
                         (BASE2, KEY2))
        self.assertEqual(keyword.last_candidate_id, "fallback:1")
        self.assertEqual(parent.last_candidate_id, "fallback:2")
        self.assertEqual(len(parent.candidates), 3)
        self.assertIsNone(parent.last_model)

    def test_invalid_first_keyword_slot_never_upgrades_to_second(self):
        parent = AIClient(config(FALLBACK_MODELS=["openai/a", "openai/b"],
                                 FALLBACK_API_BASE=BASE2 + "@" + BASE3,
                                 FALLBACK_API_KEY="@" + KEY3))
        keyword = build_keyword_client(parent)
        with self.assertRaisesRegex(ValueError, "第 1 项"):
            keyword.chat(self.messages)
        self.completion.assert_not_called()
        self.assertEqual(keyword.candidates[0].id, "fallback:1")

    def test_skipped_last_slot_preserves_real_last_error_and_clears_metadata(self):
        client = AIClient(config(FALLBACK_API_KEY="@"))
        error = SyntheticHTTPError(408)
        self.completion.side_effect = [SyntheticHTTPError(408), error]
        client.last_candidate_id = "fallback:2"
        with self.assertRaises(SyntheticHTTPError) as raised:
            client.chat(self.messages)
        self.assertIs(raised.exception, error)
        self.assertIsNone(client.last_candidate_id)
        self.assertEqual(self.completion.call_count, 2)

    def test_malformed_lists_cannot_make_any_completion_call(self):
        client = AIClient(config(FALLBACK_API_BASE=BASE2))
        self.assertFalse(client.validate_config()[0])
        with self.assertRaises(ValueError):
            client.chat(self.messages)
        self.completion.assert_not_called()

    def test_per_candidate_body_copy_isolates_mutation(self):
        body = {"chat_template_kwargs": {"enable_thinking": False}}
        received = []
        def completion(**params):
            received.append(params["extra_body"]["chat_template_kwargs"]["enable_thinking"])
            params["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
            if len(received) == 1:
                raise SyntheticHTTPError(408)
            return self.response
        self.completion.side_effect = completion
        client = AIClient(config(EXTRA_PARAMS={"extra_body": body}))
        client.chat(self.messages)
        self.assertEqual(received, [False, False])
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])

    def test_loader_uses_yaml_model_order_and_preserves_env_duplicate_names(self):
        for models in (None, "gemini/lite,gemini/lite"):
            env = {"AI_API_KEY": PRIMARY_KEY, "AI_FALLBACK_API_BASE": "@"}
            if models is not None:
                env["AI_FALLBACK_MODELS"] = models
            with self.subTest(models=models), patch.dict(os.environ, env):
                loaded = loader._load_ai_config({"ai": {"model": "gemini/main",
                                                "fallback_models": ["gemini/a", "gemini/b"]}})
                client = AIClient(loaded)
            self.assertEqual(len(client.candidates), 3)
            self.assertEqual(client.fallback_models, models.split(",") if models else ["gemini/a", "gemini/b"])

    def test_retired_file_does_not_affect_new_slots_or_open_a_secret(self):
        with patch.dict(os.environ, {"AI_MODEL": "gemini/main", "AI_API_KEY": PRIMARY_KEY,
                                    "AI_FALLBACK_MODELS": "gemini/lite", "AI_FALLBACK_API_KEY": KEY2,
                                    "AI_OPENAI_API_KEY_FILE": "/synthetic/do-not-open"}), patch.object(loader, "_read_secret_file", side_effect=AssertionError("unexpected file read")) as read:
            client = AIClient(loader._load_ai_config({}))
        self.assertFalse(client.configuration_error)
        self.assertEqual(client.candidates[1].api_key, KEY2)
        read.assert_not_called()

    def test_malformed_model_list_is_rejected_by_real_loader(self):
        with patch.dict(os.environ, {"AI_FALLBACK_MODELS": "gemini/a,,gemini/b", "AI_FALLBACK_API_BASE": "@@"}):
            with self.assertRaisesRegex(ValueError, "第 2 项"):
                loader._load_ai_config({})

    def test_shipped_config_wiring_and_missing_helper_guard(self):
        path = Path(__file__).resolve().parents[1] / "config/config.yaml"
        with patch.dict(os.environ, {"AI_MODEL": "gemini/main", "AI_API_KEY": PRIMARY_KEY,
                                    "AI_FALLBACK_MODELS": "gemini/a,gemini/b", "AI_FALLBACK_API_BASE": "@"}):
            loaded = loader.load_ai_config(str(path))
            self.assertEqual(len(AIClient(loaded).candidates), 3)
            with patch.object(loader, "merge_ai_settings", side_effect=NameError("synthetic missing helper")):
                with self.assertRaises(NameError):
                    loader.load_ai_config(str(path))
        self.completion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
