"""Export a bridge's two-channel raw binary as the existing AD3 CSV column contract."""
import argparse
import json
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path, help='ad3-<run_id> directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.capture / 'manifest.json').read_text(encoding='utf-8'))
    count = manifest['samples']
    raw = args.capture / 'samples.f64'
    if not count or raw.stat().st_size != count * 16:
        raise ValueError('No samples or raw length mismatch; preserve and inspect manifest')
    data = np.memmap(raw, dtype='<f8', mode='r', shape=(count, 2))
    with args.output.open('x', encoding='utf-8', newline='') as stream:
        stream.write(f'# Origin: {manifest["origin"]}; run_id: {manifest["run_id"]}; complete: {manifest["complete"]}\n')
        stream.write('# Time is the nominal sample clock; not synchronized UTC\n')
        stream.write('Time (s),Channel 1 (V),Channel 2 (V)\n')
        for start in range(0, count, 50000):
            end = min(start + 50000, count)
            rows = np.column_stack((np.arange(start, end) / 100000, data[start:end]))
            np.savetxt(stream, rows, delimiter=',', fmt='%.9f')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
