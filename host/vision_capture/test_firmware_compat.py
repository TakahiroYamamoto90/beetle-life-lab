"""Keep scripts/firmware_compat.json in step with firmware/xiao_esp32s3/include/config.h."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from firmware_compat import COMPAT_FILE, require, supported


def config_versions():
    """Map each build flag branch of config.h to its firmware version string."""
    text = (ROOT / 'firmware/xiao_esp32s3/include/config.h').read_text(encoding='utf-8')
    found = re.findall(r'#(?:if|elif)[^\n]*?(BLL_[A-Z_]+)[^\n]*\n\s*constexpr char kFirmwareVersion\[\] = "([^"]+)"', text)
    return dict(found)


class FirmwareCompatTests(unittest.TestCase):
    def test_current_builds_are_accepted_by_capture_tools(self):
        versions = config_versions()
        self.assertIn('BLL_EVIDENCE_STORE', versions)
        self.assertIn('BLL_PASSIVE_ADC_FRONTEND_VERIFIED', versions)
        # xiao_vision and xiao_evidence both stream ADC and capture images.
        for flag in ('BLL_EVIDENCE_STORE', 'BLL_PASSIVE_ADC_FRONTEND_VERIFIED'):
            with self.subTest(flag=flag):
                self.assertIn(versions[flag], supported('vision_capture'),
                              f'Add {versions[flag]} to {COMPAT_FILE.name} vision_capture after checking compatibility')
                self.assertIn(versions[flag], supported('piezo_capture'),
                              f'Add {versions[flag]} to {COMPAT_FILE.name} piezo_capture after checking compatibility')

    def test_known_broken_builds_stay_rejected(self):
        for broken in ('0.4.1-piezo-command-input', '0.5.0-piezo-adc-dma-8khz',
                       '0.6.0-piezo-streaming-8khz', '0.7.0-evidence-littlefs'):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported firmware'):
                require('vision_capture', broken)

    def test_edits_apply_without_reloading_the_module(self):
        (ROOT / 'captures').mkdir(exist_ok=True)  # git-ignored; absent in a fresh clone
        with tempfile.TemporaryDirectory(dir=ROOT / 'captures') as folder:
            path = Path(folder) / 'compat.json'
            data = json.loads(COMPAT_FILE.read_text(encoding='utf-8'))
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                require('vision_capture', '9.9.9-future', path)
            data['vision_capture']['versions'].append('9.9.9-future')
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(require('vision_capture', '9.9.9-future', path), '9.9.9-future')


if __name__ == '__main__':
    unittest.main()
