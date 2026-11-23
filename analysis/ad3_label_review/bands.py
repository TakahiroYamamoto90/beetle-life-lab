# 20 ms band RMS of CH1 (col 0) and CH2 (col 1) from samples.f64, offline (analysis only).
import numpy as np
from scipy import signal
from common import *
BANDS = [(20, 300), (300, 1500), (1500, 5000), (5000, 20000)]
W = 2000
SOS = [signal.butter(4, b, 'bandpass', fs=FS, output='sos') for b in BANDS]
for r in runs():
    p = OUT / r['run']
    dst = WORK / 'bands' / (r['run'] + '.npz')
    if dst.exists(): continue
    x = np.memmap(p / 'samples.f64', dtype='<f8', mode='r').reshape(-1, 2)
    n = len(x) // W * W
    res = np.zeros((n // W, len(BANDS), 2), np.float32)
    raw = np.asarray(x[:n]); raw = raw - raw[:FS].mean(axis=0)
    for i, s in enumerate(SOS):
        y = signal.sosfiltfilt(s, raw, axis=0)
        res[:, i, :] = np.sqrt((y.reshape(-1, W, 2) ** 2).mean(axis=1))
    t = (np.arange(n // W) + .5) * W / FS
    dst.parent.mkdir(exist_ok=True)
    np.savez(dst, t=t, rms=res)
    print(r['run'][:12], res.shape, flush=True)
