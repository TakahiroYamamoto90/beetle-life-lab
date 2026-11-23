"""Single source of the firmware versions each host tool accepts (firmware_compat.json).

The file is read on every check, so a long-running notebook server follows edits
without a restart. Add a version here whenever config.h gets a new one.
"""
import json
from pathlib import Path

COMPAT_FILE = Path(__file__).with_name('firmware_compat.json')


def supported(feature, path=COMPAT_FILE):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    versions = data[feature]['versions']
    if not isinstance(versions, list) or not all(isinstance(v, str) and v for v in versions):
        raise ValueError(f'Invalid firmware compatibility list: {feature}')
    return frozenset(versions)


def require(feature, firmware, path=COMPAT_FILE):
    """Raise RuntimeError naming the accepted versions when firmware is not accepted."""
    accepted = supported(feature, path)
    if firmware not in accepted:
        raise RuntimeError(
            f"Unsupported firmware {firmware or 'unknown'} for {feature}; accepted: {', '.join(sorted(accepted))}. "
            f"Add new versions to {Path(path).name} after confirming compatibility.")
    return firmware
