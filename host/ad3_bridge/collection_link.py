"""Link the AD3 bridge to the 観察ノート collection database (host/vision_capture).

- context(): registered cases for the form's drop-down, and the case / illumination of
  the most recent run as defaults.
- import_run(): after a LIVE run with XIAO forwarding ends, copy its verified observation
  images into the collection as UNREVIEWED samples (observation_import.import_run; same
  rules as the command line: XIAO hash must match, model output never sets the label,
  re-running skips frames already imported).
"""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
VISION = ROOT / 'host' / 'vision_capture'
RUN_DIR = re.compile(r'ad3-[0-9a-f]{32}')


def _modules():
    # host/vision_capture also has a server.py: never leave its folder ahead of the bridge's
    # on sys.path (observation_import inserts it at the front when first imported).
    saved = list(sys.path)
    try:
        sys.path.append(str(VISION))
        import observation_import
        from session_store import SessionStore
    finally:
        sys.path[:] = saved
    return observation_import, SessionStore


class CollectionLink:
    def __init__(self, data_dir, runs_dir):
        self.data_dir = Path(data_dir)
        self.runs_dir = Path(runs_dir)

    def recent_requests(self, limit=200):
        """Newest-first capture requests of earlier runs (only the manifest's request part)."""
        if not self.runs_dir.is_dir():
            return []
        found = []
        for path in self.runs_dir.iterdir():
            manifest = path / 'manifest.json'
            if RUN_DIR.fullmatch(path.name) and manifest.is_file():
                found.append((manifest.stat().st_mtime, manifest))
        found.sort(reverse=True)
        requests = []
        for _, manifest in found[:limit]:
            try:
                data = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if data.get('origin') == 'LIVE' and isinstance(data.get('request'), dict):
                requests.append(data['request'])
        return requests

    def context(self):
        cases, error = [], None
        try:
            _, SessionStore = _modules()
            store = SessionStore(self.data_dir, readonly=True)
            cases = [dict(case_id=c['case_id'], display_name=c.get('display_name') or '',
                          role=c.get('role') or 'unassigned', samples=c.get('samples', 0)) for c in store.cases()]
        except ValueError as exc:  # no collection database yet
            error = str(exc)
        requests = self.recent_requests()
        last_case = next((r['case_id'] for r in requests if isinstance(r.get('case_id'), str) and r['case_id']), '')
        lights = []
        for r in requests:
            value = r.get('illumination')
            if isinstance(value, str) and value and value not in lights:
                lights.append(value)
        return dict(cases=cases, last_case_id=last_case, illuminations=lights[:20],
                    last_illumination=lights[0] if lights else '', error=error)

    def diary_context(self):
        """For the 観察日記: case display names, and the latest human label and note of each
        imported photo (sample id = the XIAO action id). Read-only; labels are human, never model output."""
        try:
            _, SessionStore = _modules()
            store = SessionStore(self.data_dir, readonly=True)
            cases = {c['case_id']: c.get('display_name') or '' for c in store.cases()}
            labels = {s['id']: dict(label=s['label'], note=s.get('note') or '', reviewed_at=s.get('reviewed_at'))
                      for s in store.samples()}
        except ValueError as exc:  # no collection database yet
            return dict(cases={}, labels={}, error=str(exc))
        return dict(cases=cases, labels=labels, error=None)

    def import_run(self, run_dir, request):
        observation_import, SessionStore = _modules()
        store = SessionStore(self.data_dir)
        report = observation_import.import_run(
            store, run_dir, case_id=request.get('case_id') or '',
            illumination_profile=request.get('illumination') or '')
        session = observation_import.find_session(store, Path(run_dir).name[4:])
        return dict(status='IMPORTED', imported=len(report['imported']),
                    skipped=[dict(action_id=a, reason=r) for a, r in report['skipped']][:20],
                    session_id=session['id'] if session else None, case_id=request.get('case_id'),
                    note='Unreviewed samples in the collection; human labels are added in 観察ノート.')
