"""Bridge -> 観察ノート link: case list, defaults from earlier runs, and the end-of-run import.

Everything runs in a temporary directory; the real collection database is never opened.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from collection_link import CollectionLink  # noqa: E402
from manager import BridgeManager  # noqa: E402
import server  # noqa: E402,F401  the bridge's server.py, before vision_capture is on the path
_saved = list(sys.path)
sys.path.append(str(ROOT / 'host' / 'vision_capture'))  # after the bridge folder: its server.py differs
from session_store import SessionStore  # noqa: E402
from test_observation_import import BOOT, write_run  # noqa: E402
sys.path[:] = _saved


class CollectionLinkTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_context_lists_cases_and_defaults_to_the_last_run(self):
        link = CollectionLink(self.root, self.root / 'ad3')
        empty = link.context()
        self.assertEqual((empty['cases'], empty['last_case_id']), ([], ''))
        self.assertTrue(empty['error'])  # no collection database yet: said, not hidden
        store = SessionStore(self.root)
        store.create_session(dict(purpose='dataset', box_id='box-01'))
        run_dir = write_run(self.root, case_id='box-02')
        manifest = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
        manifest['request']['illumination'] = 'LED FRONT oblique'
        (run_dir / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        context = link.context()
        self.assertEqual([c['case_id'] for c in context['cases']], ['box-01'])
        self.assertEqual(context['last_case_id'], 'box-02')
        self.assertEqual(context['last_illumination'], 'LED FRONT oblique')

    def test_import_adds_unreviewed_samples_once_with_illumination(self):
        run_dir = write_run(self.root)
        link = CollectionLink(self.root, self.root / 'ad3')
        request = dict(case_id='box-02', illumination='LED FRONT oblique')
        first = link.import_run(run_dir, request)
        self.assertEqual((first['status'], first['imported']), ('IMPORTED', 2))
        store = SessionStore(self.root)
        [session] = store.sessions()
        self.assertEqual((session['box_id'], session['illumination_profile']), ('box-02', 'LED FRONT oblique'))
        self.assertEqual(first['session_id'], session['id'])
        self.assertEqual(store.sample(f'obs-{BOOT}-1')['label'], 'unreviewed')
        self.assertEqual(link.import_run(run_dir, request)['imported'], 0)  # re-run skips


class ManagerHookTest(unittest.TestCase):
    def test_hook_runs_only_for_live_forwarding_and_needs_a_case(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            manager = BridgeManager(Path(folder), collection_importer=lambda d, r: calls.append(d) or {})
            manager.start(dict(mode='mock', duration_s=1))
            manager.thread.join(30)
            self.assertEqual(calls, [])  # mock run: never imported
            self.assertNotIn('collection_import', manager.snapshot())


if __name__ == '__main__':
    unittest.main()
