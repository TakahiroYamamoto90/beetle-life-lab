"""Read-only regression on the same AD3 CSV; never forwards recorded data."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host/ad3_bridge'))
from processing import Processor, MODEL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path, help='existing analysis result directory')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    processor = Processor()
    rows, events = [], []
    count = 0
    for frame in pd.read_csv(args.input, comment='#', chunksize=50000):
        if list(frame.columns) != ['Time (s)', 'Channel 1 (V)', 'Channel 2 (V)']:
            raise ValueError('Unexpected CSV columns')
        data = frame.to_numpy(dtype=float)
        expected = (count + np.arange(len(data))) / 100000
        if not np.allclose(data[:, 0], expected, rtol=0, atol=1e-9):
            raise ValueError('Gap or nonzero clock origin; no bridging')
        batch, found = processor.push(data[:, 1:], count)
        count += len(data)
        rows.extend(batch); events.extend(found)
    events.extend(processor.finish())
    reference = pd.read_csv(args.reference / 'input_features_20ms.csv')
    if len(rows) != len(reference):
        raise AssertionError((len(rows), len(reference)))
    rms = np.array([r['rms_V'] for r in rows])
    expected = reference.CH2_band_rms_mV.to_numpy() / 1000
    np.testing.assert_allclose(rms, expected, rtol=1e-10, atol=1e-14)
    np.testing.assert_allclose([r['t_center_s'] for r in rows], reference.t_center_s, rtol=0, atol=1e-9)
    prior = pd.read_csv(args.reference / 'candidate_events.csv')
    prior = prior[prior.model == MODEL].to_dict('records')
    if len(events) != len(prior):
        raise AssertionError((len(events), len(prior)))
    for actual, wanted in zip(events, prior):
        for key, value in actual.items():
            if isinstance(value, (int, float)):
                np.testing.assert_allclose(value, wanted[key], rtol=1e-9, atol=1e-10)
            elif value != wanted[key]:
                raise AssertionError((key, value, wanted[key]))
    digest = hashlib.sha256()
    with args.input.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    report = dict(origin='RECORDED', forwarded=False, samples=count, feature_windows=len(rows),
                  candidate_count=len(events), candidates=events, max_rms_error_V=float(abs(rms-expected).max()),
                  input_sha256=digest.hexdigest(), config_sha256=processor.fingerprint,
                  reference=str(args.reference), comparison='same raw CSV, frozen B2 features and candidates')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
