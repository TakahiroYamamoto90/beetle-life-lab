"""Retrospective review: recorded files stay untouched, labels are append-only and sample-exact."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
import unittest
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager import BridgeManager
from review import FS, REVIEW_FILE, STATUS_FILE, TRASH_FILE, ReviewStore
from server import handler_for

RUN = 'ad3-' + 'e' * 32


@contextmanager
def recorded_run(seconds=3.0):
    root = ROOT / 'captures/test_tmp'
    root.mkdir(parents=True, exist_ok=True)
    output = root / ('ad3-review-test-' + uuid.uuid4().hex)
    run = output / RUN
    run.mkdir(parents=True)
    try:
        n = int(seconds * FS)
        t = np.arange(n) / FS
        data = np.stack([1e-4 * np.sin(2 * np.pi * 700 * t), 1e-3 * np.sin(2 * np.pi * 10 * t)], axis=1)
        data.astype('<f8').tofile(run / 'samples.f64')
        (run / 'manifest.json').write_text(json.dumps(dict(
            origin='LIVE', run_id='e' * 32, started_at_pc='2026-11-13T00:00:00+00:00', complete=True,
            request=dict(note='test run'))), encoding='utf-8')
        rows = ['t_center_s,rms_V,smoothed_V,baseline_V,score,state,reason,ch1_rms_V']
        for i in range(int(seconds / .02) - 10):
            rows.append(f'{.11 + i * .02:.2f},3e-5,3e-5,,{"" if i < 5 else 1.5},ARMED,,6e-5')
        (run / 'features.csv').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        (run / 'candidates.jsonl').write_text(json.dumps(dict(t_start_s=1.0, t_end_s=1.4, peak_score=9.0)) + '\n',
                                               encoding='utf-8')
        (run / 'annotations.jsonl').write_text(json.dumps(dict(kind='NOISE', t_s=2.0)) + '\n', encoding='utf-8')
        yield output
    finally:
        output.resolve().relative_to(root.resolve())
        shutil.rmtree(output)


def digests(run):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()
            if p.name not in (REVIEW_FILE, STATUS_FILE, TRASH_FILE)}


class ReviewTest(unittest.TestCase):
    def test_overview_wave_and_labels_leave_recorded_files_untouched(self):
        with recorded_run() as output:
            run = output / RUN
            before = digests(run)
            store = ReviewStore(output)
            runs = store.runs()
            self.assertEqual([r['run'] for r in runs], [RUN])
            self.assertEqual(runs[0]['duration_s'], 3.0)
            ov = store.overview(RUN)
            self.assertEqual(ov['biological_activity'], 'UNKNOWN')
            self.assertEqual(ov['low_activity']['schema'], 'bll.lowfreq_activity.v1')
            self.assertEqual(ov['forest_activity']['biological_activity'], 'UNKNOWN')  # forest strip (reference)
            self.assertIsInstance(ov['forest_activity']['activity_like_spans'], list)
            self.assertIsNone(ov['xiao_forest_activity'])               # no XIAO forest file in this run
            sh = ov['shadow_triggers']                                  # shadow period (reference only)
            self.assertEqual(sh['b2']['count'], len(ov['candidates']))
            self.assertEqual(sh['sources']['FOREST_XIAO'], dict(status='UNAVAILABLE'))
            self.assertEqual(sh['sources']['LOWFREQ_RATIO_3']['count'], len(ov['low_activity']['activity_like_spans']))
            self.assertNotIn('_lowfreq', ov)                    # numpy rows stay server-side
            self.assertEqual(len(ov['lowfreq']['t']), len(ov['lowfreq']['ch2']))  # 1-40 Hz RMS for the demo graph
            json.dumps(ov)
            self.assertEqual(len(ov['candidates']), 1)
            self.assertEqual(ov['live_marks'][0]['kind'], 'NOISE')
            self.assertIsNone(ov['band']['score'][0])  # warm-up has no score: missing, not zero
            self.assertGreater(max(v for v in ov['low']['ch2'][len(ov['low']['ch2']) // 4:] if v), .5)  # 10 Hz, 1 mV
            wave = store.wave(RUN, 1.0, 1.5)
            self.assertAlmostEqual(wave['t0'], 1.0)
            self.assertLessEqual(len(wave['t']), 2000)
            self.assertAlmostEqual(max(wave['ch1_max']), .1, places=2)  # mV
            first = store.annotate(RUN, dict(kind='LARVA_START', t_s=1.234567, note='動画で確認'))['record']
            self.assertEqual(first['sample'], 123457)
            self.assertEqual(first['source'], 'HUMAN_REVIEW')
            second = store.annotate(RUN, dict(kind='LARVA_END', t_s=2.5))['record']
            result = store.annotate(RUN, dict(kind='RETRACT', target_seq=first['seq']))
            self.assertEqual([m['seq'] for m in result['review_marks']], [second['seq']])
            lines = (run / REVIEW_FILE).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 3)  # retraction is appended, history kept
            self.assertEqual(digests(run), before)
            self.assertEqual(store.runs()[0]['review_marks'], 1)
            # blind labelling: kept on time labels only, never a string; plain labels carry no flag
            self.assertNotIn('blind', second)
            blind = store.annotate(RUN, dict(kind='NOISE', t_s=2.0, blind=True))['record']
            self.assertIs(blind['blind'], True)
            with self.assertRaises(ValueError):
                store.annotate(RUN, dict(kind='NOISE', t_s=2.1, blind='yes'))
            undo = store.annotate(RUN, dict(kind='RETRACT', target_seq=blind['seq'], blind=True))['record']
            self.assertNotIn('blind', undo)
            self.assertEqual(digests(run), before)

    def test_overview_shows_what_the_xiao_forest_answered(self):
        with recorded_run() as output:
            recs = [dict(kind='BEGIN', core='lowfreq-forest-core-v1', threshold=.5781),
                    dict(kind='WINDOW', t_ms=10125, p=.7, active=True, arm=False),
                    dict(kind='WINDOW', t_ms=10375, p=None, active=False, arm=True),
                    dict(kind='SPAN', t0_s=10.0, t1_s=10.25, peak_p=.7, windows=1), dict(kind='END')]
            (output / RUN / 'xiao_lowfreq.jsonl').write_text(''.join(json.dumps(r) + chr(10) for r in recs), encoding='utf-8')
            x = ReviewStore(output).overview(RUN)['xiao_forest_activity']
            self.assertEqual((x['status'], x['windows'], x['arm_windows']), ('ENDED', 2, 1))
            self.assertEqual(x['activity_like_spans'], [dict(t0_s=10.0, t1_s=10.25, peak_p=.7)])
            [trig] = ReviewStore(output).overview(RUN)['shadow_triggers']['sources']['FOREST_XIAO']['triggers']
            self.assertEqual(trig['t_s'], 10.25)  # known at the end of the span's first window
            self.assertEqual(x['biological_activity'], 'UNKNOWN')

    def test_annotation_status_starts_untouched_and_done_is_explicit(self):
        from review import STATUS_FILE
        with recorded_run() as output:
            run = output / RUN
            before = digests(run)
            store = ReviewStore(output)
            status = lambda: store.runs()[0]['review_status']
            self.assertEqual(status(), 'NOT_STARTED')
            self.assertEqual(store.overview(RUN)['review_status']['status'], 'NOT_STARTED')
            with self.assertRaises(ValueError):  # only a person reopens a DONE run
                store.set_review_status(RUN, dict(status='IN_PROGRESS'))
            first = store.annotate(RUN, dict(kind='NOISE', t_s=1.0))
            self.assertEqual(first['review_status']['status'], 'IN_PROGRESS')
            store.annotate(RUN, dict(kind='RETRACT', target_seq=first['record']['seq']))
            self.assertEqual(status(), 'IN_PROGRESS')  # removing every label is still work done
            done = store.set_review_status(RUN, dict(status='DONE'))
            self.assertEqual(done['review_status']['status'], 'DONE')
            self.assertTrue(done['review_status']['done_at_pc'])
            with self.assertRaises(ValueError):
                store.set_review_status(RUN, dict(status='DONE'))
            store.annotate(RUN, dict(kind='NOISE', t_s=2.0))
            self.assertEqual(status(), 'DONE')  # stays done until reopened
            store.set_review_status(RUN, dict(status='IN_PROGRESS'))
            self.assertEqual(status(), 'IN_PROGRESS')
            for bad in ('NOT_STARTED', 'done', None):
                with self.assertRaises(ValueError):
                    store.set_review_status(RUN, dict(status=bad))
            self.assertEqual(len((run / STATUS_FILE).read_text(encoding='utf-8').splitlines()), 2)
            self.assertEqual(digests(run), before)  # recorded files are never touched

    def test_deleted_run_goes_to_the_trash_and_comes_back_unchanged(self):
        from review import TRASH_FILE
        with recorded_run() as output:
            run = output / RUN
            before = digests(run)
            store = ReviewStore(output)
            store.annotate(RUN, dict(kind='NOISE', t_s=1.0))
            record = store.set_trashed(RUN, True, dict(reason=' 動作テスト '))
            self.assertEqual((record['deleted'], record['reason']), (True, '動作テスト'))
            self.assertEqual(store.runs(), [])
            self.assertEqual(store.diary()['entries'], [])
            for call in (lambda: store.overview(RUN), lambda: store.episodes(RUN),
                         lambda: store.annotate(RUN, dict(kind='NOISE', t_s=1.0)),
                         lambda: store.set_trashed(RUN, True, {})):
                with self.assertRaises(ValueError):
                    call()
            [row] = store.trash()
            self.assertEqual((row['run'], row['reason'], row['duration_s']), (RUN, '動作テスト', 3.0))
            store.set_trashed(RUN, False, {})
            self.assertEqual([r['run'] for r in store.runs()], [RUN])
            self.assertEqual(store.runs()[0]['review_marks'], 1)  # labels come back as they were
            self.assertEqual(store.trash(), [])
            with self.assertRaises(ValueError):
                store.set_trashed(RUN, False, {})
            self.assertEqual(len((run / TRASH_FILE).read_text(encoding='utf-8').splitlines()), 2)
            after = {k: v for k, v in digests(run).items() if k != TRASH_FILE}
            self.assertEqual(after, before)  # recorded files are never touched
            with self.assertRaises(ValueError):
                store.set_trashed('ad3-' + '0' * 32, True, {})
            with self.assertRaises(ValueError):
                store.set_trashed(RUN, True, dict(reason='x' * 301))
            busy = ReviewStore(output, busy_run=lambda: RUN)
            with self.assertRaises(BlockingIOError):
                busy.set_trashed(RUN, True, {})

    def test_moving_a_label_supersedes_it_and_keeps_history(self):
        with recorded_run() as output:
            store = ReviewStore(output)
            start = store.annotate(RUN, dict(kind='DIG_START', t_s=1.0, note='棒'))['record']
            end = store.annotate(RUN, dict(kind='DIG_END', t_s=2.0))['record']
            moved = store.annotate(RUN, dict(kind='DIG_START', t_s=0.8, note='棒', replaces_seq=start['seq']))
            self.assertEqual(moved['record']['moved_from_t_s'], 1.0)
            self.assertEqual(sorted(m['t_s'] for m in moved['review_marks']), [0.8, 2.0])
            again = store.annotate(RUN, dict(kind='DIG_START', t_s=0.7, replaces_seq=moved['record']['seq']))
            self.assertEqual(sorted(m['t_s'] for m in again['review_marks']), [0.7, 2.0])  # chain stays hidden
            for bad in (dict(kind='DIG_END', t_s=1.5, replaces_seq=start['seq']),      # already replaced
                        dict(kind='LARVA_END', t_s=1.5, replaces_seq=end['seq']),      # other kind
                        dict(kind='DIG_END', t_s=1.5, replaces_seq='2')):
                with self.assertRaises(ValueError):
                    store.annotate(RUN, bad)
            # Retracting the latest move brings the previous position back.
            undone = store.annotate(RUN, dict(kind='RETRACT', target_seq=again['record']['seq']))
            self.assertEqual(sorted(m['t_s'] for m in undone['review_marks']), [0.8, 2.0])
            self.assertEqual(len((output / RUN / REVIEW_FILE).read_text(encoding='utf-8').splitlines()), 5)
            # A lasting disturbance is a range; a point disturbance can be moved like any label.
            noise = store.annotate(RUN, dict(kind='NOISE_START', t_s=1.2))['record']
            store.annotate(RUN, dict(kind='NOISE_END', t_s=1.6))
            point = store.annotate(RUN, dict(kind='NOISE', t_s=1.4))['record']
            moved_point = store.annotate(RUN, dict(kind='NOISE', t_s=1.45, replaces_seq=point['seq']))
            kinds = sorted((m['kind'], m['t_s']) for m in moved_point['review_marks'] if m['kind'].startswith('NOISE'))
            self.assertEqual(kinds, [('NOISE', 1.45), ('NOISE_END', 1.6), ('NOISE_START', 1.2)])
            self.assertEqual(noise['kind'], 'NOISE_START')

    def test_editing_a_note_supersedes_the_label_in_place(self):
        with recorded_run() as output:
            store = ReviewStore(output)
            start = store.annotate(RUN, dict(kind='NOISE_START', t_s=1.0))['record']
            store.annotate(RUN, dict(kind='NOISE_END', t_s=2.0))
            edited = store.annotate(RUN, dict(kind='NOISE_START', t_s=start['t_s'], note='アーム戻り',
                                              replaces_seq=start['seq']))
            record = edited['record']
            self.assertEqual((record['note'], record['note_changed_from'], record['sample']), ('アーム戻り', '', start['sample']))
            self.assertNotIn('moved_from_t_s', record)  # same time: not a move
            self.assertEqual(sorted((m['kind'], m['note']) for m in edited['review_marks']),
                             [('NOISE_END', ''), ('NOISE_START', 'アーム戻り')])
            # A move keeps the note and records no note change.
            moved = store.annotate(RUN, dict(kind='NOISE_START', t_s=0.5, note='アーム戻り', replaces_seq=record['seq']))['record']
            self.assertEqual(moved['moved_from_t_s'], 1.0)
            self.assertNotIn('note_changed_from', moved)
            # Retracting the edits brings the old note back; history stays in the file.
            store.annotate(RUN, dict(kind='RETRACT', target_seq=moved['seq']))
            back = store.annotate(RUN, dict(kind='RETRACT', target_seq=record['seq']))
            self.assertEqual([m['note'] for m in back['review_marks'] if m['kind'] == 'NOISE_START'], [''])
            self.assertEqual(len((output / RUN / REVIEW_FILE).read_text(encoding='utf-8').splitlines()), 6)
            with self.assertRaises(ValueError):
                store.annotate(RUN, dict(kind='NOISE_START', t_s=1.0, note='x' * 301, replaces_seq=start['seq']))

    def test_deleting_from_the_graph_removes_both_ends_and_move_history(self):
        with recorded_run() as output:
            store = ReviewStore(output)
            start = store.annotate(RUN, dict(kind='DIG_START', t_s=1.0))['record']
            end = store.annotate(RUN, dict(kind='DIG_END', t_s=2.0))['record']
            moved = store.annotate(RUN, dict(kind='DIG_START', t_s=0.8, replaces_seq=start['seq']))['record']
            point = store.annotate(RUN, dict(kind='NOISE', t_s=1.5))['record']
            for bad in (dict(target_seqs=[]), dict(target_seqs=[moved['seq'], moved['seq']]),
                        dict(target_seqs=[start['seq']]),                       # hidden by the move
                        dict(target_seqs=[moved['seq'], end['seq'], point['seq']]), dict(target_seqs='1')):
                with self.assertRaises(ValueError):
                    store.annotate(RUN, dict(kind='RETRACT', with_history=True, **bad))
            gone = store.annotate(RUN, dict(kind='RETRACT', target_seqs=[moved['seq'], end['seq']], with_history=True))
            # The whole range is gone in one record; the position it was moved from does not come back.
            self.assertEqual([(m['kind'], m['t_s']) for m in gone['review_marks']], [('NOISE', 1.5)])
            self.assertEqual(gone['record']['target_seqs'], [moved['seq'], end['seq']])
            lines = (output / RUN / REVIEW_FILE).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 5)  # nothing removed from the file
            # Undo in the page re-adds the labels as new records at the same times.
            store.annotate(RUN, dict(kind='DIG_START', t_s=0.8))
            back = store.annotate(RUN, dict(kind='DIG_END', t_s=2.0))
            self.assertEqual(sorted(m['t_s'] for m in back['review_marks']), [0.8, 1.5, 2.0])

    def test_invalid_requests_and_running_run_are_refused(self):
        with recorded_run() as output:
            store = ReviewStore(output)
            for bad in (dict(kind='LARVA', t_s=1), dict(kind='NOISE', t_s=99), dict(kind='NOISE', t_s=float('nan')),
                        dict(kind='NOISE', t_s='1'), dict(kind='NOISE', t_s=1, note='x' * 301),
                        dict(kind='RETRACT', target_seq=7)):
                with self.assertRaises(ValueError):
                    store.annotate(RUN, bad)
            for run in ('../x', 'ad3-' + 'f' * 32, None):
                with self.assertRaises(ValueError):
                    store.overview(run)
            with self.assertRaises(ValueError):
                store.wave(RUN, 0, 61)
            self.assertFalse((output / RUN / REVIEW_FILE).exists())
            busy = ReviewStore(output, busy_run=lambda: RUN)
            self.assertEqual(busy.runs(), [])
            with self.assertRaises(BlockingIOError):
                busy.annotate(RUN, dict(kind='NOISE', t_s=1))

    def test_http_routes_need_token_for_labels(self):
        with recorded_run() as output:
            manager = BridgeManager(output)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(manager, 'test-token'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port

                def call(method, path, body=None, token='test-token'):
                    conn = HTTPConnection('127.0.0.1', port, timeout=10)
                    headers = {'Host': f'127.0.0.1:{port}', 'Content-Type': 'application/json'}
                    if token:
                        headers['X-BLL-Token'] = token
                    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = conn.getresponse()
                    data = response.read()
                    conn.close()
                    return response.status, data

                status, page = call('GET', '/review')
                self.assertEqual(status, 200)
                self.assertIn(b'test-token', page)
                status, data = call('GET', '/api/review/runs')
                self.assertEqual(json.loads(data)['runs'][0]['run'], RUN)
                self.assertEqual(call('GET', f'/api/review/overview?run={RUN}')[0], 200)
                self.assertEqual(call('GET', f'/api/review/wave?run={RUN}&t0=0&t1=x')[0], 400)
                body = dict(run=RUN, kind='SYNC_TAP', t_s=0.5)
                self.assertEqual(call('POST', '/api/review/annotate', body, token=None)[0], 403)
                self.assertEqual(call('POST', '/api/review/annotate', body)[0], 200)
                self.assertEqual(call('GET', f'/files/{RUN}/{REVIEW_FILE}')[0], 200)
            finally:
                server.shutdown()
                server.server_close()


class VideoReviewTest(unittest.TestCase):
    def test_frames_offsets_and_audio_requirement(self):
        import cv2
        with recorded_run() as output:
            run = output / RUN
            writer = cv2.VideoWriter(str(run / 'video.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), 10, (160, 90))
            for i in range(30):  # 3 s, brightness encodes the frame index
                writer.write(np.full((90, 160, 3), i * 8, np.uint8))
            writer.release()
            manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
            manifest['video'] = dict(status='RECORDED', video_file='video.mp4', recording_s_at_first_block=0.75,
                                     first_block_samples=5000)
            (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            store = ReviewStore(output)
            info = store.overview(RUN)['video']
            self.assertTrue(info['available'])
            self.assertEqual(info['frames'], 30)
            self.assertAlmostEqual(info['offset_s'], 0.7)  # 0.75 s minus the 50 ms first block
            self.assertEqual(info['offset_source'], 'ESTIMATED_PC_RECEIPT')
            self.assertTrue(store.runs()[0]['video'])
            jpeg = store.frame(RUN, 2.0, width=160)
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
            self.assertAlmostEqual(float(image.mean()), 20 * 8, delta=12)
            with self.assertRaises(LookupError):
                store.frame(RUN, 9.0)
            saved = store.annotate(RUN, dict(kind='VIDEO_OFFSET', offset_s=0.812))
            self.assertEqual(saved['video_offset']['offset_s'], 0.812)
            self.assertEqual(saved['review_marks'], [])  # offsets are not time labels
            ov = store.overview(RUN)
            self.assertEqual(ov['video']['offset_s'], 0.812)
            self.assertEqual(ov['video']['offset_source'], 'SAVED_REVIEW')
            for bad in (float('nan'), 601, '1'):
                with self.assertRaises(ValueError):
                    store.annotate(RUN, dict(kind='VIDEO_OFFSET', offset_s=bad))
            try:
                import av  # noqa: F401
            except ImportError:
                with self.assertRaises(NotImplementedError):
                    store.spectrogram(RUN)
            else:
                with self.assertRaises(LookupError):  # this test video has no audio track
                    store.spectrogram(RUN)
            store.video_cache[1].release()

    def test_black_obs_margins_are_cropped_for_display(self):
        import cv2
        with recorded_run() as output:
            run = output / RUN
            writer = cv2.VideoWriter(str(run / 'video.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), 10, (320, 176))
            for i in range(30):  # camera picture only in the top-left 160x120, like a small OBS source
                image = np.zeros((176, 320, 3), np.uint8)
                image[:120, :160] = 60 + i * 4
                writer.write(image)
            writer.release()
            manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
            manifest['video'] = dict(status='RECORDED', video_file='video.mp4')
            (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            before = hashlib.sha256((run / 'video.mp4').read_bytes()).hexdigest()
            store = ReviewStore(output)
            image = cv2.imdecode(np.frombuffer(store.frame(RUN, 1.5, width=160), np.uint8), cv2.IMREAD_GRAYSCALE)
            self.assertAlmostEqual(image.shape[0], 120, delta=6)  # margins gone, 4:3 kept
            self.assertGreater(float(image[5:-5, 5:-5].min()), 40)
            self.assertEqual(hashlib.sha256((run / 'video.mp4').read_bytes()).hexdigest(), before)
            store.video_cache[1].release()


class ArmShotTest(unittest.TestCase):
    def test_capture_times_prefer_xiao_then_pc_receipt_then_even_spacing(self):
        from review import arm_shots
        manifest = dict(acquisition=dict(sample_clock_reference=dict(pc_utc_before_ms=1794571200000,
                                                                     pc_utc_after_ms=1794571200000)))
        zero = '2026-11-13T12:00:00'

        def step(n, pose, **extra):
            return dict(action='CAPTURE_' + pose, target_pose=pose, action_id=f'obs-{"a" * 32}-{n}',
                        image_verified=True, image_url=f'/files/x/{n}.jpg',
                        result=dict(capture='SUCCESS', result_event_id=f'xiao/b/{n}'), **extra)
        with recorded_run() as output:
            run = output / RUN
            lines = [dict(candidate_seq=1, status='sending', received_at_pc=zero + '.000+00:00'),
                     dict(candidate_seq=1, status='acknowledged', received_at_pc=zero[:-2] + '30.000+00:00',
                          observation=step(1, 'FRONT'),
                          follow_ups=[step(2, 'TOP', frame_received_at_pc=zero[:-2] + '20.500+00:00'),
                                      step(3, 'BACK'), dict(action='NONE')])]
            (run / 'delivery.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
            shots = arm_shots(run, manifest, {'xiao/b/1': zero[:-2] + '04.250Z'})
            self.assertEqual([(s['pose'], s['t_s'], s['time_basis']) for s in shots],
                             [('FRONT', 4.25, 'XIAO_RESULT_EVENT'), ('TOP', 20.5, 'PC_FRAME_RECEIPT'),
                              ('BACK', 30.0, 'ESTIMATED_EVEN_SPACING')])
            self.assertEqual(arm_shots(run, {}, {}), [])  # no sample clock reference: no times


class ActuationSpanTest(unittest.TestCase):
    def test_arm_spans_from_receipt_lines_or_from_xiao_decision_and_result_times(self):
        from review import actuation_near, actuation_spans
        manifest = dict(acquisition=dict(sample_clock_reference=dict(pc_utc_before_ms=1794571200000,
                                                                     pc_utc_after_ms=1794571200000)))
        at = lambda s: f'2026-11-13T12:00:{s:06.3f}+00:00'
        with recorded_run() as output:
            run = output / RUN
            lines = [dict(kind='MOVE_BEGIN', action_id='a1', pose='TOP', received_at_pc=at(10)),
                     dict(kind='CAPTURE_BEGIN', action_id='a1', pose='TOP', received_at_pc=at(11.5)),
                     dict(kind='PARK_BEGIN', received_at_pc=at(20)),
                     dict(kind='PARK_DONE', elapsed_ms='2100', received_at_pc=at(22.1)),
                     dict(kind='PARK_DONE', elapsed_ms='1500', received_at_pc=at(40), late_read_possible=True)]
            (run / 'actuation.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
            spans = actuation_spans(run, manifest)
            self.assertEqual([(s['kind'], s['t0_s'], s['t1_s']) for s in spans],
                             [('MOVE', 10.0, 11.5), ('PARK', 20.0, 22.1), ('PARK', 38.5, 40.0)])
            self.assertTrue(spans[2]['late_read_possible'])  # begin unseen: the XIAO's own duration
            self.assertEqual(actuation_near(spans, 11.0, 11.2)['relation'], 'DURING')
            self.assertEqual(actuation_near(spans, 23.0, 23.5), dict(relation='AFTER', seconds=0.9, kind='PARK'))
            self.assertIsNone(actuation_near(spans, 30.0, 30.5))
            self.assertEqual(actuation_spans(run, {}), [])
            # Older runs: decision -> result of each capture step from a saved Evidence export.
            (run / 'actuation.jsonl').unlink()
            step = dict(action='CAPTURE_FRONT', target_pose='FRONT', action_id='a2', decision_event_id='x/d/1',
                        result=dict(result_event_id='x/r/2'))
            (run / 'delivery.jsonl').write_text(json.dumps(dict(candidate_seq=1, status='acknowledged',
                                                                observation=step)) + '\n', encoding='utf-8')
            times = {'x/d/1': at(5)[:-6] + 'Z', 'x/r/2': at(8.25)[:-6] + 'Z'}
            spans = actuation_spans(run, manifest, times)
            self.assertEqual([(s['kind'], s['t0_s'], s['t1_s'], s['time_basis']) for s in spans],
                             [('MOVE_AND_CAPTURE', 5.0, 8.25, 'XIAO_DECISION_TO_RESULT')])
            # With the capture duration: the capture is cut off (ramp + settle remain) ...
            spans = actuation_spans(run, manifest, times, {'x/r/2': 1000})
            self.assertEqual([(s['kind'], s['t0_s'], s['t1_s'], s['time_basis']) for s in spans],
                             [('MOVE', 5.0, 7.25, 'XIAO_DECISION_TO_CAPTURE_START')])
            # ... and a settle-only step (arm already at the pose, the XIAO skipped the move) is no arm movement.
            self.assertEqual(actuation_spans(run, manifest, times, {'x/r/2': 2000}), [])
            # A fixed camera has no arm.
            (run / 'delivery.jsonl').write_text(json.dumps(dict(candidate_seq=1, status='acknowledged', observation=dict(
                step, action='CAPTURE_FIXED', target_pose='FIXED'))) + '\n', encoding='utf-8')
            self.assertEqual(actuation_spans(run, manifest, times), [])
            # The review files are never written by this.
            self.assertFalse((run / REVIEW_FILE).exists())

    def test_capture_durations_are_read_from_evidence_exports(self):
        from review import evidence_result_times
        with recorded_run() as output:
            folder = output / 'evidence'
            folder.mkdir()
            base = dict(origin='LIVE', time_quality='ESTIMATED', timestamp_utc='2026-11-13T12:00:05.000Z')
            events = [dict(base, event_id='x/d/1', event_type='POLICY_DECISION'),
                      dict(base, event_id='x/r/2', event_type='OBSERVATION_ACTION_RESULT',
                           result=dict(capture=dict(duration_ms=1410))),
                      dict(base, event_id='x/r/3', event_type='OBSERVATION_ACTION_RESULT',
                           result=dict(capture=dict(duration_ms='1410'))),
                      dict(base, event_id='x/r/4', event_type='OBSERVATION_ACTION_RESULT', origin='SIMULATED',
                           result=dict(capture=dict(duration_ms=900)))]
            (folder / 'evidence-a.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
            capture_ms = {}
            times = evidence_result_times([folder], capture_ms)
            self.assertEqual(sorted(times), ['x/d/1', 'x/r/2', 'x/r/3'])
            self.assertEqual(capture_ms, {'x/r/2': 1410})  # numbers only, LIVE only
            self.assertEqual(sorted(evidence_result_times([folder])), ['x/d/1', 'x/r/2', 'x/r/3'])


class PlayableVideoTest(unittest.TestCase):
    def test_mkv_is_stream_copied_to_mp4_and_served_with_ranges(self):
        import cv2
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest('PyAV is not installed')
        with recorded_run() as output:
            run = output / RUN
            writer = cv2.VideoWriter(str(run / 'video.mkv'), cv2.VideoWriter_fourcc(*'mp4v'), 10, (160, 96))
            for i in range(30):
                writer.write(np.full((96, 160, 3), 40 + i * 4, np.uint8))
            writer.release()
            manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
            manifest['video'] = dict(status='RECORDED', video_file='video.mkv')
            (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            before = digests(run)
            store = ReviewStore(output)
            cache = output.parent / 'review_cache'
            try:
                mp4 = store.playable_video(RUN)
                self.assertEqual((mp4.parent, mp4.suffix), (cache, '.mp4'))
                self.assertEqual(store.playable_video(RUN), mp4)  # cached
                self.assertEqual(digests(run), before)  # the run folder is not written
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(BridgeManager(output), 'test-token', store))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                    host = {'Host': f'127.0.0.1:{server.server_port}'}
                    conn.request('GET', f'/api/review/video?run={RUN}', headers=dict(host, Range='bytes=10-19'))
                    response = conn.getresponse()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), mp4.read_bytes()[10:20])
                    self.assertEqual(response.getheader('Content-Range'), f'bytes 10-19/{mp4.stat().st_size}')
                    conn.request('GET', f'/api/review/video?run={RUN}', headers=host)
                    response = conn.getresponse()
                    self.assertEqual((response.status, response.getheader('Content-Type')), (200, 'video/mp4'))
                    self.assertEqual(len(response.read()), mp4.stat().st_size)
                    conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
            finally:
                shutil.rmtree(cache, ignore_errors=True)


class EpisodeViewTest(unittest.TestCase):
    def test_episode_cards_join_candidate_steps_shots_and_answers_read_only(self):
        manifest_extra = dict(acquisition=dict(sample_clock_reference=dict(pc_utc_before_ms=1794571200000,
                                                                           pc_utc_after_ms=1794571200000)))
        zero = '2026-11-13T12:00:0'
        with recorded_run() as output:
            run = output / RUN
            manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
            manifest.update(manifest_extra, request=dict(note='test run', case_id='box-9'))
            (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (run / 'candidates.jsonl').write_text(
                json.dumps(dict(candidate_seq=1, t_start_s=1.0, t_end_s=1.4, peak_score=9.0)) + '\n' +
                json.dumps(dict(candidate_seq=2, t_start_s=2.2, t_end_s=2.4, peak_score=5.0)) + '\n', encoding='utf-8')
            step = dict(action='CAPTURE_FRONT', target_pose='FRONT', action_id='obs-' + 'a' * 32 + '-1',
                        reason_codes=['PASSIVE_CANDIDATE_NEW'], image_verified=True, image_url='/files/x/1.jpg',
                        frame_received_at_pc=zero + '1.900+00:00', result=dict(capture='SUCCESS'))
            lines = [dict(candidate_seq=1, status='sending', received_at_pc=zero + '1.500+00:00'),
                     dict(candidate_seq=1, status='acknowledged', observation=step, follow_ups=[dict(action='NONE')]),
                     dict(candidate_seq=2, status='acknowledged',
                          observation=dict(action='WAIT', reason_codes=['SELF_ACTUATION_RECENT']))]
            (run / 'delivery.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
            store = ReviewStore(output)
            store.low_dir = output / 'lowfreq-cache'  # outside RUN_RE: not listed as a run
            # 観察日記 before any cache: the low-frequency reference is computed in the background
            self.assertEqual(store.diary()['entries'][0]['low_activity'], dict(status='PENDING'))
            store.low_thread.join(30)
            self.assertEqual(store.diary()['entries'][0]['low_activity']['schema'], 'bll.lowfreq_activity.v1')
            store.annotate(RUN, dict(kind='DIG_START', t_s=0.5))
            store.annotate(RUN, dict(kind='DIG_END', t_s=1.2))
            store.annotate(RUN, dict(kind='NOISE', t_s=2.9))
            before = digests(run)
            data = store.episodes(RUN)
            self.assertEqual(digests(run), before)
            self.assertEqual((data['case_id'], data['biological_activity']), ('box-9', 'UNKNOWN'))
            self.assertEqual(data['summary'], dict(candidates=2, captures=1, waits=1, answered=2))
            first, second = data['episodes']
            self.assertEqual([(s['action'], s['pose'], s['t_s']) for s in first['steps']], [('CAPTURE_FRONT', 'FRONT', 1.9)])
            self.assertEqual(first['answers'], ['DIG'])
            self.assertEqual(second['answers'], ['NOISE'])  # point within 1 s after the candidate
            self.assertEqual(second['steps'][0]['action'], 'WAIT')
            self.assertEqual(first['clip_s'], [0.0, 3.0])  # 5 s before (clamped) to the end of the run
            self.assertIsNone(first['clip_video_s'])  # no video in this run
            self.assertTrue(all(e['biological_activity'] == 'UNKNOWN' for e in data['episodes']))
            self.assertEqual(data['low_activity']['biological_activity'], 'UNKNOWN')
            self.assertIsInstance(first['low_activity'], list)
            # The 観察日記 entry of the same run: counts, verified photos, answers; read-only, LIVE only.
            diary = store.diary()
            self.assertEqual(digests(run), before)
            self.assertEqual((diary['mock_runs'], diary['biological_activity']), (0, 'UNKNOWN'))
            entry, = diary['entries']
            self.assertEqual((entry['case_id'], entry['note'], entry['candidates'], entry['captures'], entry['waits'],
                              entry['failed'], entry['answered']), ('box-9', 'test run', 2, 1, 1, 0, 2))
            self.assertEqual(entry['answer_counts'], dict(LARVA=0, DIG=1, NOISE=1))
            self.assertEqual(entry['photos'], [dict(action_id='obs-' + 'a' * 32 + '-1', url='/files/x/1.jpg',
                                                    pose='FRONT', candidate_seq=1)])
            manifest['origin'] = 'SYNTHETIC'
            (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual((store.diary()['entries'], store.diary()['mock_runs']), ([], 1))


class AiOfTest(unittest.TestCase):
    def test_model_output_for_display_only_when_present(self):
        from review import ai_of
        step = dict(vision_ai=dict(model=dict(recipe='v13b-96'), prediction=dict(
            status='MODEL_OUTPUT_UNVERIFIED', top=dict(class_name='VISIBLE_LARVA', score=93))))
        self.assertEqual(ai_of(step), dict(class_name='VISIBLE_LARVA', score=93, recipe='v13b-96'))
        self.assertIsNone(ai_of(dict(vision_ai=dict(prediction=dict(status='MODEL_NOT_VERIFIED')))))
        self.assertIsNone(ai_of(dict()))


class PageScriptTest(unittest.TestCase):
    def test_bridge_page_scripts_parse(self):
        """A syntax error stops the whole page (e.g. stuck at 'AD3を確認中'); check with node."""
        import re
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        folder = ROOT / 'captures/test_tmp' / ('page-check-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        try:
            pages = [Path(__file__).with_name('index.html'), Path(__file__).with_name('review.html'),
                     Path(__file__).with_name('episodes.html'), Path(__file__).with_name('demo.html'),
                     Path(__file__).with_name('diary.html'),
                     ROOT / 'host' / 'vision_capture' / 'index.html']
            for file in pages:
                name = file.parent.name + '-' + file.name
                page = file.read_text(encoding='utf-8')
                for i, script in enumerate(re.findall(r'<script>(.*?)</script>', page, re.S)):
                    path = folder / f'{name}.{i}.js'
                    path.write_text(script, encoding='utf-8')
                    result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, f'{name} script {i}: {result.stderr[:400]}')
        finally:
            shutil.rmtree(folder)

    def test_demo_timeline_follows_the_recorded_times(self):
        """/demo: the 6-step marks of an episode come from the run's own times (candidate span,
        arm MOVE/PARK spans, capture times); a WAIT lights 判断 only; wording keeps UNKNOWN."""
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        module = (ROOT / 'host/web/demo/timeline.js').as_uri()
        script = ('import(process.argv[1]).then(m=>{'
                  'globalThis.BllObservationView=require(process.argv[2]);'
                  'const ep={t_start_s:10,t_end_s:11,clip_s:[5,30],ch2_x:4,ch1_x:1.2,delivery:"acknowledged",steps:['
                  '{action:"CAPTURE_TOP",action_id:"a1",pose:"TOP",reason_codes:["PASSIVE_CANDIDATE_NEW","TOP_NOT_RECENTLY_OBSERVED"],capture:"SUCCESS",t_s:14,ai:{class_name:"VISIBLE_LARVA",score:93,recipe:"v13b-96"}},'
                  '{action:"CAPTURE_BACK",action_id:"a2",pose:"BACK",reason_codes:["EPISODE_FOLLOW_UP"],capture:"FAILED",t_s:18}]};'
                  'const spans=[{kind:"MOVE",action_id:"a1",pose:"TOP",t0_s:11.2,t1_s:13.2},'
                  '{kind:"MOVE",action_id:"a2",pose:"BACK",t0_s:15.5,t1_s:17.4},{kind:"PARK",t0_s:19,t1_s:21.5}];'
                  'const marks=m.buildTimeline(ep,spans);'
                  'const wait=m.buildTimeline({t_start_s:3,t_end_s:4,clip_s:[0,10],steps:[{action:"WAIT",reason_codes:["SELF_ACTUATION_RECENT"]}]},[]);'
                  'console.log(JSON.stringify({marks:marks.map(x=>[x.t,x.kind]),at:[9,10.5,12,13.5,14.5,15,17.6,20,22].map(t=>m.stagesOf(m.markAt(marks,t))),'
                  'why:ep.steps.map(m.whyText),result:[m.resultText(ep,marks[4]),m.resultText(ep,marks[8]),m.resultText(ep,marks[10])],done:[m.doneStages(marks,marks[4]).sort(),m.doneStages(marks,marks[6]).sort()],'
                  'wait:wait.map(x=>x.kind),waitStages:m.stagesOf(m.markAt(wait,5)),waitWhy:m.whyText({action:"WAIT",reason_codes:["SELF_ACTUATION_RECENT"]})}))})')
        result = subprocess.run([node, '-e', script, module, str(Path(__file__).with_name('observation_view.js'))],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        out = json.loads(result.stdout)
        self.assertEqual(out['marks'], [[10, 'SENSE'], [11, 'DECIDE'], [11.2, 'MOVE'], [13.2, 'CAPTURE'], [14, 'RESULT'],
                                        [14.75, 'DECIDE'], [15.5, 'MOVE'], [17.4, 'CAPTURE'], [18, 'RESULT'],
                                        [19, 'PARK'], [21.5, 'END']])
        self.assertEqual(out['at'], [[], [1], [4], [5], [6], [2, 3], [5], [4], []])
        self.assertEqual(out['why'][0], '上からはしばらく見ていないので、上から確認します')
        self.assertEqual(out['why'][1], '同じ揺れの続きとして、後ろからも確認します')
        self.assertIn('画像AI：幼虫が見えた（確度 93）', out['result'][0])  # model output on the demo (no 参考 since 2026-11-23)
        self.assertEqual(out['result'][2], '画像AI：上からの写真に幼虫が写っていると判定（確度 93）')
        self.assertIn('いない、という意味ではありません', out['result'][1])
        self.assertEqual(out['done'], [[1, 2, 3, 4, 5], [1, 2, 3]])  # passed stages of the current decide cycle
        self.assertEqual(out['wait'], ['SENSE', 'WAIT', 'END'])
        self.assertEqual(out['waitStages'], [2, 3])
        self.assertEqual(out['waitWhy'], 'カメラを動かした直後なので、少し待っています')

    def test_demo_flow_path_follows_the_marks(self):
        """/demo state-transition diagram: the path comes from the episode marks, with the follow-up
        loop (REC -> 続けて見る？ -> MEM), the park and the WAIT short loop back to monitoring."""
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        flow = (ROOT / 'host/web/demo/flow.js').as_uri()
        script = ('import(process.argv[1]).then(f=>{'
                  'const k=["SENSE","DECIDE","MOVE","CAPTURE","RESULT","DECIDE","MOVE","CAPTURE","RESULT","PARK","END"];'
                  'const marks=k.map((kind,i)=>({t:i,kind}));const wait=["SENSE","WAIT","END"].map((kind,i)=>({t:i,kind}));'
                  'console.log(JSON.stringify({r:f.flowPath(marks,marks[4]),d:f.flowPath(marks,marks[5]).slice(-4),'
                  'e:f.flowPath(marks,marks[10]).slice(-4),w:f.flowPath(wait,wait[2]),none:f.flowPath(marks,null),'
                  't:f.flowTimes(marks),edges:f.FLOW_EDGES.map(e=>e[0]+">"+e[1])}))})')
        result = subprocess.run([node, '-e', script, flow], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        out = json.loads(result.stdout)
        self.assertEqual(out['r'], ['MON', 'SEN', 'MEM', 'DEC', 'ACT', 'OBS', 'REC'])
        self.assertEqual(out['d'], ['REC', 'FOL', 'MEM', 'DEC'])
        self.assertEqual(out['e'], ['REC', 'FOL', 'HOME', 'MON'])
        self.assertEqual(out['w'], ['MON', 'SEN', 'MEM', 'DEC', 'WAIT', 'MON'])
        self.assertEqual(out['none'], ['MON'])
        self.assertEqual((out['t']['SEN'], out['t']['ACT'], out['t']['FOL'], out['t']['HOME']), (0, 2, 5, 9))
        path = out['r'] + ['FOL', 'MEM', 'DEC', 'WAIT', 'MON']
        for a, b in zip(path, path[1:]):  # every step of a path is a drawn edge
            self.assertIn(f'{a}>{b}', out['edges'])

    def test_demo_memory_history_and_state_come_from_the_records(self):
        """/demo memory panel: the history is the run's records in time order; the state is a count/age
        summary; which state a decision used comes only from the XIAO's reason codes."""
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        mem = (ROOT / 'host/web/demo/memory.js').as_uri()
        script = ('import(process.argv[1]).then(m=>{'
                  'const ep={candidate_seq:1,t_start_s:10,t_end_s:11,steps:['
                  '{action:"CAPTURE_FRONT",action_id:"a1",pose:"FRONT",reason_codes:["FRONT_NOT_RECENTLY_OBSERVED"],capture:"SUCCESS",t_s:14},'
                  '{action:"CAPTURE_TOP",action_id:"a2",pose:"TOP",reason_codes:["EPISODE_FOLLOW_UP","FRONT_RECENTLY_CHECKED"],capture:"FAILED",t_s:18}]};'
                  'const arm=[{kind:"MOVE",action_id:"a2",pose:"TOP",t0_s:15,t1_s:17},{kind:"PARK",t0_s:19,t1_s:21}];'
                  'const h=m.buildHistory([ep],arm);const s=m.stateAt(h,arm,20,ep,null);'
                  'console.log(JSON.stringify({kinds:h.map(x=>x.kind),front:s.views.FRONT,top:s.views.TOP,back:s.views.BACK,'
                  'fail:s.failures,arm:s.arm,epi:s.episode,used:m.codesOf(h,16,1)}))})')
        result = subprocess.run([node, '-e', script, mem], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        out = json.loads(result.stdout)
        self.assertEqual(out['kinds'], ['SENSE', 'DECIDE', 'RESULT', 'DECIDE', 'RESULT', 'PARK'])
        self.assertEqual(out['front'], dict(age=6, ok=True))
        self.assertEqual(out['top'], dict(age=2, ok=False))
        self.assertIsNone(out['back'])
        self.assertEqual(out['fail'], 1)
        self.assertEqual(out['arm'], dict(moving=True, kind='PARK'))  # parking at t=20 (no pose on this span)
        self.assertEqual(out['epi'], dict(seq=1, done=2, planned=2))
        self.assertEqual(out['used']['codes'], ['EPISODE_FOLLOW_UP', 'FRONT_RECENTLY_CHECKED'])

    def test_demo_live_state_follows_the_bridge_snapshot(self):
        """/demo LIVE: the mark comes from the bridge state - detector inside an event, the
        candidate being sent, the arm lines the XIAO printed, then its stored answer."""
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        module = (ROOT / 'host/web/demo/live.js').as_uri()
        script = ('import(process.argv[1]).then(m=>{'
                  'const cand={candidate_seq:3,t_start_s:20,t_end_s:21,context:{ch2_x:5,ch1_x:1.1}};'
                  'const base={running:true,forwarding:true,elapsed_s:22,detector_state:"MONITORING",candidates:[cand],observations:[],arm_live:[]};'
                  'const obs=[{candidate_seq:3,action:"CAPTURE_TOP",action_id:"a",target_pose:"TOP",reason_codes:["TOP_NOT_RECENTLY_OBSERVED"],'
                  'result:{capture:"SUCCESS"},image_url:"/x.jpg",image_verified:true}];'
                  'const k=s=>{const r=m.liveState(s);return r.mark&&r.mark.kind};'
                  'console.log(JSON.stringify({'
                  'stopped:k({...base,running:false}),sense:k({...base,detector_state:"CANDIDATE"}),'
                  'pending:k({...base,delivering:3}),move:m.liveState({...base,delivering:3,arm_live:[{kind:"MOVE_BEGIN",pose:"TOP"}]}).mark,'
                  'capture:k({...base,delivering:3,arm_live:[{kind:"MOVE_BEGIN",pose:"TOP"},{kind:"CAPTURE_BEGIN",pose:"TOP"}]}),'
                  'park:k({...base,delivering:3,observations:obs,arm_live:[{kind:"PARK_BEGIN"}]}),'
                  'result:m.liveState({...base,observations:obs},22),later:k({...base,observations:obs,elapsed_s:40},22),'
                  'unsent:m.liveState({...base,forwarding:false}).mark,'
                  'wait:k({...base,observations:[{candidate_seq:3,action:"WAIT",reason_codes:["SELF_ACTUATION_RECENT"]}]},22),'
                  'unverified:m.stepOf({...obs[0],image_verified:false}).image_url}))})')
        result = subprocess.run([node, '-e', script, module], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        out = json.loads(result.stdout)
        self.assertIsNone(out['stopped'])
        self.assertEqual(out['sense'], 'SENSE')
        self.assertEqual(out['pending'], 'DECIDE')
        self.assertEqual(out['move'], {'kind': 'MOVE', 'pose': 'TOP'})
        self.assertEqual(out['capture'], 'CAPTURE')
        self.assertEqual(out['park'], 'PARK')
        self.assertEqual(out['result']['mark'], {'kind': 'RESULT', 'step': 0, 'pose': 'TOP'})
        self.assertEqual(out['result']['ep']['steps'][0]['image_url'], '/x.jpg')
        self.assertEqual(out['result']['ep']['ch2_x'], 5)
        self.assertIsNone(out['later'])  # back to watching after the hold time
        self.assertEqual(out['unsent'], {'kind': 'DECIDE', 'none': True, 'pending': False})
        self.assertEqual(out['wait'], 'WAIT')
        self.assertIsNone(out['unverified'])  # an image without a matching XIAO hash is not shown

    def test_diary_sentences_count_records_and_keep_unknown(self):
        """/diary: the sentence of an entry only counts what the records hold; human photo
        labels are summarised as human checks; days are newest first, entries oldest first."""
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        module = (ROOT / 'host/web/diary/text.js').as_uri()
        script = ('import(process.argv[1]).then(m=>{'
                  'const e={run:"r1",started_at_pc:"2026-11-14T15:56:57+00:00",duration_s:72,candidates:3,forwarded:true,captures:3,failed:1,waits:1,'
                  'answered:2,answer_counts:{LARVA:1,DIG:0,NOISE:1},photos:[{action_id:"a",pose:"FRONT"},{action_id:"b",pose:"TOP"},{action_id:"c",pose:"TOP"}]};'
                  'const labels={a:{label:"larva_visible"},b:{label:"background"},c:{label:"unreviewed"}};'
                  'const e2={...e,run:"r2",started_at_pc:"2026-11-14T16:30:00+00:00",candidates:0,photos:[]};'
                  'const e3={...e,run:"r3",started_at_pc:"2026-11-13T10:00:00+00:00",forwarded:false,captures:0,failed:0,waits:0,answered:0,photos:[]};'
                  'const days=m.groupByDay([e2,e,e3]);'
                  'console.log(JSON.stringify({s:m.entrySentence(e,labels),quiet:m.entrySentence(e2),unsent:m.entrySentence(e3),'
                  'days:days.map(d=>[d.key,d.entries.map(x=>x.run),d.totals.candidates]),title:days[0].title,dur:[m.duration(72),m.duration(5400)]}))})')
        result = subprocess.run([node, '-e', script, module], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        out = json.loads(result.stdout)
        self.assertEqual(out['s'], '気になる揺れが 3 回ありました。前・上から写真を 3 枚撮りました。1 回は撮影できませんでした。'
                                   '1 回は撮らずに待ちました。振り返りで 2 回を答え合わせ済み（幼虫活動 1・外乱（声・物音） 1）。'
                                   '写真は人が 2 枚確認し、幼虫が見えたのは 1 枚でした。')
        self.assertEqual(out['quiet'], '静かでした。気になる揺れはありませんでした。')
        self.assertIn('送っていないため、撮影はしていません', out['unsent'])
        self.assertEqual(out['days'], [['2026-11-15', ['r1', 'r2'], 3], ['2026-11-13', ['r3'], 3]])  # JST dates
        self.assertIn('11月15日', out['title'])
        self.assertEqual(out['dur'], ['1分12秒', '1時間30分'])
        for text in (out['s'], out['quiet'], out['unsent']):
            self.assertNotIn('幼虫が動', text)
            self.assertNotIn('いません。', text.replace('撮影はしていません。', ''))

    def test_page_modules_parse_and_every_import_is_exported(self):
        """ES modules under host/web (all pages): syntax, each named import exists in its module
        (a missing export stops the whole page with nothing but a console error), and no
        module assigns a name it imports (imported bindings are read-only)."""
        import re
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        static = ROOT / 'host' / 'web'
        files = sorted(static.rglob('*.js'))
        self.assertTrue(files)
        for file in files:
            result = subprocess.run([node, '--check', str(file)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f'{file.name}: {result.stderr[:400]}')
        # Modules other than main.js load in Node (no DOM work at load time); list their exports.
        # The classic page scripts they read at load time are provided as on the page.
        loader = ('const fs=require("fs"),url=require("url");'
                  f'globalThis.BllObservationView=require({json.dumps(str(Path(__file__).with_name("observation_view.js")))});'
                  '(async()=>{const out={};'
                  'for(const f of JSON.parse(process.argv[1])){out[f]=Object.keys(await import(url.pathToFileURL(f)))}'
                  'console.log(JSON.stringify(out))})().catch(e=>{console.error(e.stack);process.exit(1)})')
        loadable = [str(f) for f in files if f.name != 'main.js']
        result = subprocess.run([node, '-e', loader, json.dumps(loadable)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[:600])
        exports = {Path(k).resolve(): set(v) for k, v in json.loads(result.stdout).items()}
        for file in files:
            text = file.read_text(encoding='utf-8')
            for names, target in re.findall(r"^import \{([^}]*)\} from '([^']+)';", text, re.M):
                path = (file.parent / target).resolve()
                self.assertIn(path, exports, f'{file.name}: {target} not found')
                for name in [n.strip().split(' as ')[0] for n in names.split(',')]:
                    self.assertIn(name, exports[path], f'{file.name} imports {name} from {target}')
                    assign = re.search(r'(?<!const )(?<!let )(?<!var )(?<![\w$.])' + re.escape(name)
                                       + r'\s*(=(?![=>])|\+\+|--|[-+*/%|&^]=)|(\+\+|--)' + re.escape(name) + r'(?![\w$])', text)
                    self.assertIsNone(assign, f'{file.parent.name}/{file.name} assigns imported {name}')
        pages = {Path(__file__).with_name('review.html'): ('review/main', '__TOKEN__'),
                 Path(__file__).with_name('index.html'): ('live/main', '__TOKEN__'),
                 Path(__file__).with_name('episodes.html'): ('episodes/main', None),
                 Path(__file__).with_name('demo.html'): ('demo/main', None),
                 Path(__file__).with_name('demo_prev.html'): ('demo_prev/main', None),  # comparison snapshot
                 Path(__file__).with_name('diary.html'): ('diary/main', None),
                 ROOT / 'host' / 'vision_capture' / 'index.html': ('notes/main', '__BLL_TOKEN__')}
        for file, (main, token) in pages.items():
            page = file.read_text(encoding='utf-8')
            self.assertIn(f'<script type="module" src="/static/{main}.js"></script>', page, file.name)
            self.assertTrue((static / (main + '.js')).is_file(), main)
            self.assertNotIn('<script>', page, f'{file.name}: inline script left')
            if token:
                self.assertIn(f'<meta name="bll-token" content="{token}">', page, file.name)


if __name__ == '__main__':
    unittest.main()
