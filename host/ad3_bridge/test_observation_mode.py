"""Observation mode follow / explore (firmware 0.18.1 'observe-mode'): the bridge asks the MCU,
reads its answer, and records which rule is in force, also for firmware without the command."""
import unittest

import manager
import protocol


class FakePort:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = b''

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def readline(self, size):
        return self.replies.pop(0) if self.replies else b''


def link_with(replies):
    link = protocol.XiaoLink.__new__(protocol.XiaoLink)
    link.port, link.actuation = FakePort(replies), []
    return link


class SetModeTest(unittest.TestCase):
    def test_reply_unknown_command_and_error(self):
        link = link_with([b'HEARTBEAT uptime_ms=1\n', b'OBSERVATION_MODE mode=explore\n'])
        self.assertEqual(link.set_mode('explore'), 'explore')
        self.assertEqual(link.port.written, b'observe-mode explore\n')
        self.assertIsNone(link_with([b'OBSERVATION_ERROR reason=unknown_command\n']).set_mode('follow'))
        with self.assertRaises(RuntimeError):
            link_with([b'OBSERVATION_ERROR reason=adc_busy\n']).set_mode('follow')
        with self.assertRaises(ValueError):
            link_with([]).set_mode('random')
        self.assertEqual(link_with([b'OBSERVATION_MODE mode=hold\n']).set_mode('hold'), 'hold')

    def test_arm_lines_read_meanwhile_are_kept(self):
        link = link_with([b'ARM PARK_DONE pose=FRONT\n', b'OBSERVATION_MODE mode=follow\n'])
        self.assertEqual(link.set_mode('follow'), 'follow')


class ObservationModeTest(unittest.TestCase):
    class Link:
        def __init__(self, policy, answer=None, error=None, has_command=True):
            self.state = dict(observation_policy=policy)
            self.answer, self.error = answer, error
            if not has_command:
                self.set_mode = None

        def set_mode(self, mode):
            if self.error:
                raise self.error
            return self.answer

    def test_applied_unsupported_and_error(self):
        self.assertEqual(manager.observation_mode(self.Link('observation-policy-v4', 'explore'), 'explore'),
                         dict(requested='explore', effective='explore', status='APPLIED'))
        # 0.18.0 always follows, 0.17.0 always explores: recorded as their own rule
        self.assertEqual(manager.observation_mode(self.Link('observation-policy-v4'), 'explore')['effective'], 'follow')
        self.assertEqual(manager.observation_mode(self.Link('observation-policy-v3'), 'follow'),
                         dict(requested='follow', effective='explore', status='UNSUPPORTED_FIRMWARE'))
        failed = manager.observation_mode(self.Link('observation-policy-v4', error=TimeoutError('MODE_REPLY_TIMEOUT')), 'follow')
        self.assertEqual((failed['status'], failed['effective']), ('ERROR', None))

    def test_request_validation(self):
        bridge = manager.BridgeManager.__new__(manager.BridgeManager)
        with self.assertRaisesRegex(ValueError, 'observation_mode'):
            bridge.start(dict(mode='mock', duration_s=1, observation_mode='random'))


if __name__ == '__main__':
    unittest.main()
