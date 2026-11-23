"""Streaming adaptation of frozen research code; research files remain unchanged."""
from collections import deque
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'analysis/passive_detection'))
from improved import ContinuousCandidate
from pipeline import make_sos

FS = 100000
MODEL = 'B2_guarded_mad'
# A candidate longer than max_candidate_s (e.g. long digging, or a level step the detector cannot
# follow) is kept as INCOMPLETE and the frozen detector is rebuilt with a new warm-up, instead of
# stopping the whole acquisition (2026-11-16, user-approved). Other faults still stop it.
RESET_FAULTS = ('MAX_DURATION_MANUAL_RESET_REQUIRED',)


def configuration():
    detector_path = ROOT / 'analysis/passive_detection/improved_config.json'
    preprocessing_path = ROOT / 'analysis/passive_detection/config.json'
    cfg = json.loads(detector_path.read_text(encoding='utf-8'))
    pre = json.loads(preprocessing_path.read_text(encoding='utf-8'))
    # Fail closed if research configuration changes; porting requires a comparison.
    for value, expected in ((cfg, 'ad7ce5c0316a136a4c17d73d8ffd5d23faeadb510d1566d79253d8b4286a9825'),
                            (pre, 'c7de664b37ce953d8d8ee1ab522850a6c9c8744b4d1a66d31971f618a298af90')):
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        if hashlib.sha256(canonical).hexdigest() != expected:
            raise ValueError('Research configuration changed; regression review required')
    metadata = dict(detector=cfg, preprocessing=pre, sample_rate_hz=FS, piezo_channel=2,
                    adapter='bll.ad3_bridge.v1', rearm_timeout_s=30,
                    fault_policy=dict(reset_and_continue=list(RESET_FAULTS), other='STOP_ACQUISITION'),
                    code_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ('analysis/passive_detection/improved.py',
                                              'analysis/passive_detection/pipeline.py',
                                              'host/ad3_bridge/processing.py')})
    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return cfg, pre, metadata, fingerprint


class Processor:
    def __init__(self):
        cfg, pre, self.metadata, self.fingerprint = configuration()
        if cfg['window_s'] != .02 or pre['primary_band_hz'] != [300, 1500] or pre['trim_s'] != .1:
            raise ValueError('Unsupported preprocessing configuration')
        self.cfg = cfg
        self.sos = make_sos(FS, pre, pre['primary_band_hz'], False)
        self.zi = np.zeros((len(self.sos), 2, 2))
        self.first = None
        self.count = 0
        self.window_index = 0
        self.skip = round(pre['trim_s'] * FS)
        self.n = round(cfg['window_s'] * FS)
        self.tail_windows = round(pre['trim_s'] / cfg['window_s'])
        self.pending = np.empty((0, 2))
        self.tail = deque()
        self.detector = ContinuousCandidate(cfg, cfg['models'][MODEL])
        self.rearm_start = None
        self.resets = []  # [{t_s, reason}] detector rebuilds after a reset-and-continue fault

    def push(self, values, sample_index):
        if sample_index != self.count or values.ndim != 2 or values.shape[1] != 2 or not len(values):
            raise ValueError('Missing or invalid sample block')
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite samples')
        if self.first is None:
            self.first = values[0].copy()
        filtered, self.zi = signal.sosfilt(self.sos, values - self.first, axis=0, zi=self.zi)
        ignore = max(0, min(len(values), self.skip - self.count))
        self.count += len(values)
        self.pending = np.concatenate((self.pending, filtered[ignore:]))
        complete = len(self.pending) // self.n
        if not complete:
            return [], []
        windows = self.pending[:complete * self.n].reshape(complete, self.n, 2)
        rms = np.sqrt(np.mean(windows * windows, axis=1))
        self.pending = self.pending[complete * self.n:].copy()
        rows, events = [], []
        for value in rms:
            center = (self.skip + (self.window_index + .5) * self.n) / FS
            self.window_index += 1
            self.tail.append((center, value))
            # Delay the final 100 ms, matching offline extract()'s tail exclusion.
            if len(self.tail) <= self.tail_windows:
                continue
            t, value = self.tail.popleft()
            self.detector.update(t, float(value[1]))
            trace = self.detector.trace.pop()
            rows.append(dict(trace, ch1_rms_V=float(value[0])))
            events.extend(self.detector.events)
            self.detector.events.clear()
            if self.detector.state == 'FAULT':
                if self.detector.fault_reason not in RESET_FAULTS:
                    raise ValueError('DETECTOR_FAULT ' + str(self.detector.fault_reason))
                # The open candidate was already closed as INCOMPLETE (FAULT:...) and is in events.
                # Warm-up assumes the new background is unstimulated; that is not proof of quiet.
                self.resets.append(dict(t_s=t, reason=self.detector.fault_reason))
                self.detector = ContinuousCandidate(self.cfg, self.cfg['models'][MODEL])
                self.rearm_start = None
                continue
            if self.detector.state == 'REARMING':
                if self.rearm_start is None:
                    self.rearm_start = t
                if t - self.rearm_start >= 30:
                    raise ValueError('REARM_TIMEOUT_MANUAL_RESTART_REQUIRED')
            else:
                self.rearm_start = None
        return rows, events

    def finish(self):
        self.detector.finish(self.count / FS)
        events = list(self.detector.events)
        self.detector.events.clear()
        return events  # unfinished candidates stay PC-only
