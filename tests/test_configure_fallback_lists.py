"""Offline V2 fallback-list menu contracts; no AI imports, sockets or services."""
import builtins
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

with mock.patch("socket.socket.connect", side_effect=AssertionError("network during menu import")), \
        mock.patch("socket.create_connection", side_effect=AssertionError("network during menu import")):
    from deploy.docker import docker_configure as docker

shared = docker.shared
from envfile import ConfigError, read_env, write_env
from native_config import NativeApplication
from deploy.docker.runtime_config import parse_runtime_env

ROOT = Path(__file__).resolve().parents[1]
MODELS = "AI_FALLBACK_MODELS"
BASE = "AI_FALLBACK_API_BASE"
KEY = "AI_FALLBACK_API_KEY"
PUBLIC_URL = "https://relay.example.invalid/v1"
PUBLIC_BASES = "@" + PUBLIC_URL
OLD_KEYS = "@synthetic-old-list-key"
NEW_KEYS = "@ synthetic-new-list-key $literal 'quoted' "
VALUES = {
    "EMAIL_FROM": "sender@example.invalid", "EMAIL_TO": "reader@example.invalid",
    "EMAIL_PASSWORD": "synthetic-mail-secret", "TZ": "UTC", "STORAGE_BACKEND": "local",
    "AI_ANALYSIS_ENABLED": "true", "AI_MODEL": "gemini/synthetic-main",
    "AI_API_KEY": "synthetic-primary-secret",
    MODELS: "gemini/synthetic-backup,openai/@cf/synthetic-third",
    "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30", "AI_FUTURE_OPTION": "synthetic-future-value",
}
CSRF = re.compile(r'name="_csrf" value="([^"]+)"')


class FallbackListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config"),
        }, clear=True))
        self.guards = [self.patch(mock.patch(target, side_effect=AssertionError("offline-only test")))
                       for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                                      "socket.getaddrinfo", "subprocess.run", "subprocess.Popen", "os.system",
                                      "smtplib.SMTP", "smtplib.SMTP_SSL")]
        self.runner = mock.Mock(side_effect=AssertionError("service control forbidden"))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()
        self.runner.assert_not_called()

    def application(self, kind, values):
        path = self.root / kind / "runtime" / "env"
        syntax = "docker" if kind == "legacy-docker" else "linux"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic retained comment\n", encoding="utf-8")
        write_env(path, values, syntax)
        if kind == "docker":
            app = docker.DockerApplication(path)
        else:
            app = NativeApplication(path, app_dir=self.root, unit_dir=self.root / "units", runner=self.runner)
        return app, path, syntax

    def assert_private(self, text):
        for secret in ("synthetic-old-list-key", "synthetic-new-list-key", "synthetic-mail-secret",
                       "synthetic-primary-secret", "synthetic-url-user", "synthetic-url-password",
                       "synthetic-url-token", "synthetic-legacy-key"):
            self.assertNotIn(secret, text)

    def menu(self, kind, app, edits, *, save=True, decline=False, invalid=False):
        sections = docker.SECTIONS if kind == "docker" else shared.MENU_SECTIONS
        answers, hidden, active = [], [], None
        for key, value in edits:
            choice = next(number for number, (_title, keys) in sections.items() if key in keys)
            if active != choice:
                if active is not None:
                    answers.append("0")
                answers.append(choice)
                active = choice
            answers.append(str(sections[choice][1].index(key) + 1))
            (hidden if key in shared.SECRET_FIELDS else answers).append(value)
        if active is not None:
            answers.append("0")
        answers.append("5")
        if invalid:
            answers += ["s", "q", "y"]
        elif save:
            answers += ["s", "y"]
        elif decline:
            answers += ["s", "n", "q", "y"]
        else:
            answers += ["q", "y"]
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers) as entered, \
                mock.patch.object(shared.getpass, "getpass", side_effect=hidden) as password, \
                contextlib.redirect_stdout(output):
            saved = docker.configure_terminal(app) if kind == "docker" else shared.configure_terminal(
                app.document.path, application=app)
        text = output.getvalue() + str(entered.call_args_list) + str(password.call_args_list)
        self.assert_private(text)
        self.assertEqual(password.call_count, len(hidden))
        return saved, text

    def web(self, kind, app, requests, before_request=None):
        """Run the actual handlers in memory, retaining disk snapshots per request."""
        module = docker if kind == "docker" else shared
        path = app.document.path
        requests = iter(requests)
        responses, snapshots = [], []

        class Server:
            server_port = 8765

            def __init__(self, _address, handler):
                self.handler = handler

            def call(self, method, form):
                request = self.handler.__new__(self.handler)
                body = urllib.parse.urlencode(form).encode("utf-8")
                request.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
                                   "Content-Length": str(len(body))}
                request.rfile, request.wfile = io.BytesIO(body), io.BytesIO()
                statuses = []
                request.send_response = statuses.append
                request.send_header = lambda *_: None
                request.end_headers = lambda: None
                getattr(request, "do_" + method)()
                return statuses[0], request.wfile.getvalue().decode("utf-8")

            def handle_request(self):
                if before_request is not None:
                    before_request(len(responses))
                method, form = next(requests)
                if method == "POST":
                    form = dict(form, _csrf=CSRF.search(self.call("GET", {})[1])[1])
                responses.append(self.call(method, form))
                snapshots.append(path.read_bytes() if path.exists() else None)

            def server_close(self):
                pass

        with mock.patch.object(module, "HTTPServer", Server), contextlib.redirect_stdout(io.StringIO()) as output:
            if kind == "docker":
                args = SimpleNamespace(host="127.0.0.1", port=0, public_port=0, ssh_user="synthetic",
                                       ssh_host="server.example.invalid", ssh_port=22)
                saved = docker.serve(app, args)
            else:
                saved = shared.serve(path, "127.0.0.1", 0, 0, "synthetic", "server.example.invalid", 22,
                                     "docker" if kind == "legacy-docker" else "linux",
                                     application=None if kind == "legacy-docker" else app)
        self.assert_private(output.getvalue() + "".join(page for _, page in responses))
        return saved, responses, snapshots

    def test_main_fields_and_password_metadata_match_both_deployments(self):
        values = dict(VALUES, **{BASE: PUBLIC_BASES, KEY: OLD_KEYS})
        for section in (shared.MENU_SECTIONS["2"][1], docker.SECTIONS["2"][1]):
            for key in (MODELS, BASE, KEY):
                self.assertIn(key, section)
            self.assertFalse(any(key.startswith("AI_OPENAI_") for key in section))
        self.assertIn(KEY, shared.SECRET_FIELDS)
        self.assertTrue(docker.field_policy(KEY).sensitive)
        self.assertFalse(docker.field_policy(BASE).sensitive)
        for page in (shared.render(values), shared.render(values, deployment="docker"), docker.render(values)):
            self.assertIn(f'name="{KEY}" type="password" value=""', page)
            self.assertIn(f'name="{BASE}" type="text" value="{PUBLIC_BASES}"', page)
            for phrase in ("英文逗号", "开头、末尾和连续 @ 保留空位", "单值内不能包含 @", "不支持转义",
                           "留空保持已有整列", ":clear", "同 provider、同端点"):
                self.assertIn(phrase, page)
            for key in (BASE, KEY):
                field = re.search(r'<input name="' + key + r'"[^>]*>', page)[0]
                self.assertNotIn(" required", field)
            self.assert_private(page)
        self.assertTrue(all(not re.fullmatch(r"AI_FALLBACK_(?:API_BASE|API_KEY)_?\d+", key)
                            for key in shared.FIELD_MAP))

    def test_fixed_stdlib_helper_loads_without_importing_ai_package_or_dependencies(self):
        original_import = builtins.__import__
        forbidden = {"trendradar", "litellm", "yaml", "requests", "boto3", "feedparser"}

        def guarded_import(name, *args, **kwargs):
            if name.split(".", 1)[0] in forbidden:
                raise AssertionError("configuration imported an application dependency")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("isolated_list_configure", ROOT / "deploy/configure.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", side_effect=guarded_import):
            spec.loader.exec_module(module)
            self.assertEqual(module.validate(dict(VALUES, **{BASE: PUBLIC_BASES})), [])
            self.assertIn(f'name="{KEY}" type="password"', module.render(VALUES))
        loaded = sys.modules["_trendradar_menu_ai_config"]
        self.assertEqual(Path(loaded.__file__).resolve(), ROOT / "trendradar/ai_config.py")

    def test_full_validation_reuses_the_shared_env_key_contract(self):
        values = dict(VALUES, **{KEY: OLD_KEYS})
        with mock.patch.object(shared, "validate_fallback_settings", return_value=["synthetic binding error"]) as validator:
            self.assertIn("synthetic binding error", shared.validate(values))
        validator.assert_called_once_with(values)

    def test_valid_empty_slots_and_either_column_activation_can_be_saved(self):
        models = "gemini/one,openai/@cf/two"
        cases = [
            (models, "", ""), (models, " ", " \t "),
            (models, PUBLIC_BASES, ""), (models, "", OLD_KEYS),
            (models, "@", ""), (models, "", "@"), (models, "@", "@"),
            (models, PUBLIC_URL + "@", "key-one@"), (models, " \t ", "@key-two"),
            ("gemini/one,gemini/two,openai/three", "@@" + PUBLIC_URL, "@@key-three"),
            ("gemini/one,gemini/two,openai/three", PUBLIC_URL + "@@", "key-one@@"),
            ("openai/same,openai/same", PUBLIC_URL + "@https://other.example.invalid/v1", "@"),
        ]
        for model_list, bases, keys in cases:
            with self.subTest(models=model_list, bases=bases, key_slots=keys.count("@") + 1):
                values = dict(VALUES, **{MODELS: model_list, BASE: bases, KEY: keys})
                self.assertEqual(shared.validate(values), [])
                self.assertEqual(docker.validate(values), [])
        # Independent missing keys are runtime skip conditions, not save blockers.
        values = dict(VALUES, **{MODELS: "openai/another", BASE: PUBLIC_URL})
        self.assertEqual(shared.validate(values), [])
        self.assertEqual(docker.validate(values), [])

    def test_bad_binding_counts_models_or_legacy_conflicts_are_value_safe(self):
        cases = [
            {BASE: PUBLIC_URL}, {KEY: "synthetic-new-list-key"},
            {BASE: "@@"}, {KEY: "@synthetic-new-list-key@"},
            {MODELS: "", BASE: "@"}, {MODELS: "", KEY: "@"},
            {MODELS: "gemini/one,,openai/two", BASE: "@@"},
            {MODELS: ",openai/two", BASE: "@"},
            {MODELS: "gemini/one,", KEY: "@"},
            {MODELS: "no-provider,openai/two", KEY: "@"},
            {MODELS: "gemini/one,openai/", BASE: "@"},
        ]
        for updates in cases:
            with self.subTest(fields=list(updates)):
                values = dict(VALUES, **updates)
                for errors in (shared.validate(values), docker.validate(values)):
                    self.assertTrue(errors)
                    self.assert_private("\n".join(errors))
                    self.assertNotIn(PUBLIC_URL, "\n".join(errors))
                    self.assertNotIn("/synthetic/unread-key-file", "\n".join(errors))
        # Empty Action secrets do not activate the new format or conflict with legacy auth.
        old = dict(VALUES, **{BASE: " \t ", KEY: "", "AI_OPENAI_API_KEY": "synthetic-legacy-key"})
        self.assertEqual(shared.validate(old), [])

    def test_every_url_slot_is_validated_and_credential_fragments_never_render(self):
        invalid = [
            "ftp://relay.example.invalid/v1", "https:///missing-host", "https://[broken",
            "https://relay.example.invalid:99999/v1", "relay.example.invalid/v1",
            "https://synthetic-url-user:synthetic-url-password@relay.example.invalid/v1",
            "https://relay.example.invalid/path with space", "https://relay.example.invalid/#synthetic-url-token",
            "https://relay.example.invalid/\nsynthetic-url-token", "https://relay.example.invalid/\0synthetic-url-token",
        ]
        for url in invalid:
            for bases in (url + "@" + PUBLIC_URL, PUBLIC_URL + "@" + url):
                with self.subTest(case=invalid.index(url), sensitive_slot=bases.startswith(PUBLIC_URL)):
                    errors = shared.validate_field(BASE, bases)
                    self.assertTrue(errors)
                    self.assertNotIn(bases, "\n".join(errors))
                    self.assert_private("\n".join(errors))
                    self.assertTrue(shared.url_field_never_renders(BASE, bases))
                    values = dict(VALUES, **{BASE: bases, KEY: OLD_KEYS})
                    for page in (shared.render(values, errors), docker.render(values, errors)):
                        self.assertNotIn(bases, page)
                        self.assert_private(page)
                        self.assertIn(f'name="{BASE}" type="text" value=""', page)
                    for module in (shared, docker):
                        self.assert_private("\n".join(module.change_lines({}, values)))
        queried = PUBLIC_URL + "@https://other.example.invalid/v1?access=synthetic-url-token"
        self.assertEqual(shared.validate_field(BASE, queried), [])
        self.assertTrue(shared.url_field_never_renders(BASE, queried))
        self.assertTrue(docker.credential_url(BASE, queried))
        for module in (shared, docker):
            self.assert_private(module.render(dict(VALUES, **{BASE: queried})))
            self.assertNotIn(queried, module.display_value(BASE, queried))

    def test_terminal_key_add_replace_clear_keep_and_cancel_preserve_whole_column(self):
        cases = [("", NEW_KEYS, NEW_KEYS, "将新增"), (OLD_KEYS, NEW_KEYS, NEW_KEYS, "将替换"),
                 (OLD_KEYS, ":clear", "", "将清空"), (OLD_KEYS, "", OLD_KEYS, "没有待保存变更"),
                 (OLD_KEYS, ":cancel", OLD_KEYS, "没有待保存变更")]
        for kind in ("linux", "docker"):
            for old, entered, expected, operation in cases:
                with self.subTest(kind=kind, operation=operation):
                    app, path, _ = self.application(kind, dict(VALUES, **{BASE: PUBLIC_BASES, KEY: old}))
                    saved, text = self.menu(kind, app, [(KEY, entered)])
                    self.assertTrue(saved)
                    self.assertIn(operation, text)
                    self.assertIn(shared.FALLBACK_LIST_HELP, text)
                    loaded = read_env(path)
                    self.assertEqual(loaded[KEY], expected)
                    self.assertEqual(loaded[BASE], PUBLIC_BASES)
                    self.assertEqual(loaded["AI_FUTURE_OPTION"], VALUES["AI_FUTURE_OPTION"])
                    self.assertIn("# synthetic retained comment", path.read_text())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(parse_runtime_env(path.read_bytes())[KEY], expected)

    def test_terminal_can_empty_one_slot_without_clearing_the_column(self):
        for kind in ("linux", "docker"):
            app, path, _ = self.application(kind, dict(VALUES, **{BASE: PUBLIC_URL + "@", KEY: "key-one@"}))
            saved, _ = self.menu(kind, app, [(BASE, PUBLIC_BASES), (KEY, OLD_KEYS)])
            self.assertTrue(saved)
            self.assertEqual(read_env(path)[BASE], PUBLIC_BASES)
            self.assertEqual(read_env(path)[KEY], OLD_KEYS)

    def test_terminal_bad_counts_and_missing_models_do_not_write_or_backup(self):
        for kind in ("linux", "docker"):
            for edit in ((BASE, PUBLIC_URL), (KEY, "synthetic-new-list-key"), (MODELS, ":clear")):
                app, path, _ = self.application(kind, dict(VALUES, **{BASE: PUBLIC_BASES, KEY: OLD_KEYS}))
                before, backups = path.read_bytes(), sorted(path.parent.glob(".env.backups/*"))
                saved, _ = self.menu(kind, app, [edit], invalid=True)
                self.assertFalse(saved)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(path.parent.glob(".env.backups/*")), backups)

    def test_terminal_cancel_or_declined_save_preserves_disk_and_backups(self):
        for kind in ("linux", "docker"):
            for decline in (False, True):
                app, path, _ = self.application(kind, dict(VALUES, **{BASE: PUBLIC_BASES, KEY: OLD_KEYS}))
                before, backups = path.read_bytes(), sorted(path.parent.glob(".env.backups/*"))
                saved, _ = self.menu(kind, app, [(KEY, NEW_KEYS), (BASE, ":clear")], save=False, decline=decline)
                self.assertFalse(saved)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(path.parent.glob(".env.backups/*")), backups)

    def test_web_preview_get_and_blank_save_retain_public_base_and_private_key_lists(self):
        for kind in ("linux", "docker", "legacy-docker"):
            app, path, syntax = self.application(kind, VALUES)
            before = path.read_bytes()
            saved, responses, snapshots = self.web(kind, app, [
                ("POST", {BASE: PUBLIC_BASES, KEY: NEW_KEYS, "WEEKLY_TIME": "12:30", "_action": "preview"}),
                ("GET", {}),
                ("POST", {MODELS: "", BASE: "", KEY: "", "WEEKLY_TIME": "12:30"}),
            ])
            self.assertTrue(saved)
            self.assertEqual([code for code, _ in responses], [200, 200, 200])
            self.assertEqual(snapshots[:2], [before, before])
            loaded = read_env(path, syntax)
            self.assertEqual(loaded[MODELS], VALUES[MODELS])
            self.assertEqual(loaded[BASE], PUBLIC_BASES)
            self.assertEqual(loaded[KEY], NEW_KEYS)
            self.assertEqual(loaded["AI_FUTURE_OPTION"], VALUES["AI_FUTURE_OPTION"])

    def test_web_structural_errors_keep_secret_drafts_until_repaired(self):
        for kind in ("linux", "docker", "legacy-docker"):
            app, path, syntax = self.application(kind, VALUES)
            before = path.read_bytes()
            saved, responses, snapshots = self.web(kind, app, [
                ("POST", {BASE: PUBLIC_URL, KEY: NEW_KEYS, "WEEKLY_TIME": "12:30"}),
                ("GET", {}),
                ("POST", {BASE: PUBLIC_BASES, KEY: "", "WEEKLY_TIME": "12:30"}),
            ])
            self.assertTrue(saved)
            self.assertEqual([code for code, _ in responses], [400, 200, 200])
            self.assertEqual(snapshots[:2], [before, before])
            self.assertEqual(read_env(path, syntax)[KEY], NEW_KEYS)
            self.assertEqual(read_env(path, syntax)[BASE], PUBLIC_BASES)

    def test_web_missing_models_new_old_conflicts_and_sensitive_urls_cancel_cleanly(self):
        cases = [
            {MODELS: "", BASE: "@", KEY: NEW_KEYS},
            {BASE: PUBLIC_URL + "@https://[broken?access=synthetic-url-token", KEY: NEW_KEYS},
        ]
        for kind in ("linux", "docker", "legacy-docker"):
            for updates in cases:
                app, path, _ = self.application(kind, dict(VALUES, **updates))
                before, backups = path.read_bytes(), sorted(path.parent.glob(".env.backups/*"))
                saved, responses, snapshots = self.web(kind, app, [
                    ("GET", {}), ("POST", {BASE: "", KEY: "", "WEEKLY_TIME": "12:30"}),
                    ("POST", {"_action": "cancel"}),
                ])
                self.assertFalse(saved)
                self.assertEqual([code for code, _ in responses], [200, 400, 200])
                self.assertEqual(snapshots, [before] * 3)
                self.assertEqual(sorted(path.parent.glob(".env.backups/*")), backups)

    def test_retired_fields_do_not_block_a_new_list_save(self):
        for kind in ("linux", "docker", "legacy-docker"):
            values = dict(VALUES, AI_OPENAI_API_KEY="synthetic-legacy-key")
            app, path, syntax = self.application(kind, values)
            saved, responses, snapshots = self.web(kind, app, [
                ("POST", {BASE: PUBLIC_BASES, KEY: NEW_KEYS, "WEEKLY_TIME": "12:30"}),
            ])
            self.assertTrue(saved)
            self.assertEqual([code for code, _ in responses], [200])
            self.assertEqual(read_env(path, syntax)[KEY], NEW_KEYS)

    def test_web_clear_all_columns_survives_error_get_and_omitted_retry(self):
        for kind in ("linux", "docker", "legacy-docker"):
            app, path, syntax = self.application(kind, dict(VALUES, **{BASE: PUBLIC_BASES, KEY: OLD_KEYS}))
            before = path.read_bytes()
            saved, responses, snapshots = self.web(kind, app, [
                ("POST", {MODELS: ":clear", BASE: ":clear", KEY: ":clear", "AI_TIMEOUT": "0", "WEEKLY_TIME": "12:30"}),
                ("GET", {}), ("POST", {"AI_TIMEOUT": "10", "WEEKLY_TIME": "12:30"}),
            ])
            self.assertTrue(saved)
            self.assertEqual([code for code, _ in responses], [400, 200, 200])
            self.assertEqual(snapshots[:2], [before, before])
            loaded = read_env(path, syntax)
            self.assertTrue(all(loaded[key] == "" for key in (MODELS, BASE, KEY)))

    def test_docker_never_offers_or_introduces_native_file_fields_with_new_lists(self):
        for kind in ("docker", "legacy-docker"):
            app, path, syntax = self.application(kind, dict(VALUES, **{BASE: PUBLIC_BASES, KEY: OLD_KEYS}))
            saved, responses, _ = self.web(kind, app, [("POST", {
                "AI_API_KEY_FILE": "/synthetic/submitted-main", "AI_OPENAI_API_KEY_FILE": "/synthetic/submitted-old",
                "AI_FALLBACK_API_KEY_FILE": "/synthetic/submitted-list", "WEEKLY_TIME": "12:30",
            })])
            self.assertTrue(saved)
            loaded = read_env(path, syntax)
            for key in ("AI_API_KEY_FILE", "AI_OPENAI_API_KEY_FILE", "AI_FALLBACK_API_KEY_FILE"):
                self.assertNotIn(key, loaded)
                self.assertNotIn(key, responses[0][1])
            self.assertNotIn("/synthetic/submitted", responses[0][1])

    def test_concurrent_env_change_still_rejects_new_list_save_without_overwriting(self):
        for kind in ("linux", "docker", "legacy-docker"):
            app, path, _ = self.application(kind, VALUES)
            before, backups = path.read_bytes(), sorted(path.parent.glob(".env.backups/*"))
            changed = before + b"# concurrent synthetic edit\n"

            def mutate(index):
                if index == 1:
                    path.write_bytes(changed)

            saved, responses, snapshots = self.web(kind, app, [
                ("GET", {}), ("POST", {BASE: PUBLIC_BASES, KEY: NEW_KEYS, "WEEKLY_TIME": "12:30"}),
                ("POST", {"_action": "cancel"}),
            ], before_request=mutate)
            self.assertFalse(saved)
            self.assertEqual([code for code, _ in responses], [200, 409, 200])
            self.assertEqual(snapshots, [before, changed, changed])
            self.assertEqual(sorted(path.parent.glob(".env.backups/*")), backups)

    def test_legacy_terminal_prompt_preserves_new_key_column_and_uses_getpass(self):
        for entered, expected in ((NEW_KEYS, NEW_KEYS), ("", OLD_KEYS), (":cancel", OLD_KEYS), (":clear", "")):
            with mock.patch.object(shared.getpass, "getpass", return_value=entered) as hidden, \
                    mock.patch("builtins.input", side_effect=AssertionError("visible password input")):
                value = shared._prompt_field(*shared.FIELD_MAP[KEY], {KEY: OLD_KEYS})
            self.assertEqual(value, expected)
            self.assert_private(str(hidden.call_args))


    def test_yaml_fallback_defaults_can_be_saved_by_each_real_menu(self):
        for kind in ("linux", "docker", "legacy-docker"):
            app, path, syntax = self.application(kind, dict(VALUES, **{MODELS: "", BASE: PUBLIC_BASES, KEY: OLD_KEYS}))
            config = app.app_dir / "config" / "config.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("ai:\n  model: gemini/synthetic-main\n  fallback_models:\n    - gemini/synthetic-backup\n    - openai/synthetic-third\n")
            result, transcript = self.menu(kind, app, [])
            self.assertTrue(result)
            self.assertEqual(read_env(path, syntax)[MODELS], "")
            self.assertEqual(read_env(path, syntax)[BASE], PUBLIC_BASES)
            self.assert_private(transcript)

    def test_menu_yaml_count_mismatch_is_rejected(self):
        config = self.root / "custom.yaml"
        config.write_text("ai:\n  fallback_models:\n    - gemini/one\n    - gemini/two\n")
        values = dict(VALUES, **{MODELS: "", BASE: PUBLIC_URL})
        self.assertTrue(shared.validate(values, config_path=config))
        self.assertTrue(docker.validate(values, config_path=config))

    def test_lean_yaml_parser_matches_common_runtime_defaults(self):
        from ai_settings import _lean_ai_defaults
        self.assertEqual(_lean_ai_defaults("ai:\n  model: gemini/main\n  api_base: ''\n  fallback_models: []\n"),
                         {"model": "gemini/main", "api_base": "", "fallback_models": ""})
        self.assertEqual(_lean_ai_defaults("ai:\n  fallback_models:\n    - gemini/one\n    - openai/two\n"),
                         {"fallback_models": ["gemini/one", "openai/two"]})

    def test_lean_yaml_rejects_flow_alias_and_merge_instead_of_losing_defaults(self):
        from ai_settings import _lean_ai_defaults
        from native_schedule import ScheduleError
        for value in ('ai: {model: gemini/main}', 'ai: *defaults',
                      'ai:\n  <<: *defaults\n  model: gemini/main'):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                _lean_ai_defaults(value)

    def test_yaml_defaults_are_used_by_web_save_without_copying_models_to_env(self):
        for kind in ("linux", "docker", "legacy-docker"):
            values = dict(VALUES, **{MODELS: "", BASE: PUBLIC_BASES, KEY: OLD_KEYS})
            if kind == "legacy-docker":
                # The standalone setup handler has no application object;
                # an explicit path represents this fixture's separate checkout.
                values["CONFIG_PATH"] = str(self.root / "config" / "config.yaml")
            app, path, syntax = self.application(kind, values)
            config = app.app_dir / "config" / "config.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("ai:\n  fallback_models:\n    - gemini/synthetic-backup\n    - openai/synthetic-third\n")
            saved, responses, snapshots = self.web(kind, app, [("POST", {"WEEKLY_TIME": "12:30"})])
            self.assertTrue(saved)
            self.assertEqual([code for code, _ in responses], [200])
            self.assertEqual(read_env(path, syntax)[MODELS], "")


if __name__ == "__main__":
    unittest.main()
