"""Release metadata/dispatch contracts. No GitHub or registry calls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / '.github/scripts/image_release_metadata.py'


class ReleaseWorkflowTests(unittest.TestCase):
    def test_same_version_dispatch_is_main_only_and_serialized(self):
        workflow = yaml.load((ROOT / '.github/workflows/release-image.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn('workflow_dispatch', workflow['on'])
        self.assertIn('refs/heads/main', workflow['jobs']['image']['if'])
        self.assertEqual(workflow['concurrency']['cancel-in-progress'], 'false')
        self.assertEqual(workflow['on']['push']['tags'], ['v*'])

    def test_version_and_immutable_sha_tags_are_published(self):
        workflow = yaml.load((ROOT / '.github/workflows/release-image.yml').read_text(), Loader=yaml.BaseLoader)
        steps = workflow['jobs']['image']['steps']
        metadata = next(step for step in steps if step.get('id') == 'meta')['with']
        self.assertIn('steps.version.outputs.image_tag', metadata['tags'])
        self.assertIn('type=raw,value=latest', metadata['tags'])
        self.assertIn('type=sha,format=long,prefix=sha-', metadata['tags'])
        self.assertIn('org.opencontainers.image.version=', metadata['labels'])
        self.assertTrue(any('image_release_metadata.py' in step.get('run', '') for step in steps))


@unittest.skipIf(sys.version_info < (3, 11), 'release helper runs on workflow Python 3.12')
class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('release_metadata_test', HELPER)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'trendradar').mkdir()
        self.put_version('26.9')

    def put_version(self, version, package_version=None):
        (self.root / 'pyproject.toml').write_text('[project]\nversion = ' + repr(version) + '\n')
        (self.root / 'trendradar/__init__.py').write_text('__version__ = ' + repr(package_version or version) + '\n')

    def test_main_rebuild_and_matching_tag_use_unchanged_version(self):
        self.assertEqual(self.module.resolve_tag(self.root, 'refs/heads/main', 'workflow_dispatch'), 'v26.9')
        self.assertEqual(self.module.resolve_tag(self.root, 'refs/tags/v26.9', 'push'), 'v26.9')

    def test_wrong_ref_event_or_package_version_is_rejected(self):
        for ref, event in [('refs/heads/topic', 'workflow_dispatch'), ('refs/tags/v26.9', 'workflow_dispatch'),
                           ('refs/tags/v26.10', 'push'), ('refs/heads/main', 'push'),
                           ('refs/heads/main', 'pull_request')]:
            with self.subTest(ref=ref, event=event), self.assertRaises(ValueError):
                self.module.resolve_tag(self.root, ref, event)
        self.put_version('26.9', '26.10')
        with self.assertRaises(ValueError):
            self.module.resolve_tag(self.root, 'refs/heads/main', 'workflow_dispatch')

    def test_invalid_image_tag_is_rejected(self):
        for version in ('26.9/other', '26.9 token', 'x' * 128):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.put_version(version)
                self.module.resolve_tag(self.root, 'refs/heads/main', 'workflow_dispatch')

    def test_cli_produces_only_validated_github_output(self):
        env = dict(os.environ, GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch')
        result = subprocess.run([sys.executable, str(HELPER)], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'image_tag=' + self.module.resolve_tag(ROOT, 'refs/heads/main', 'workflow_dispatch') + '\n')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
