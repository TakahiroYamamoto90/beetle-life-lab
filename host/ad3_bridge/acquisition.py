"""WaveForms 3.25.1 AnalogIn record adapter. Never configures output instruments."""
import ctypes as ct
import os
from pathlib import Path
import time

import numpy as np

SDK_VERSION = '3.25.1'
SAMPLE_RATE = 100000


class AcquisitionError(RuntimeError):
    pass


class WaveForms:
    def __init__(self, dll_path=None):
        path = dll_path or os.environ.get('BLL_DWF_DLL') or str(
            Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Digilent/WaveForms3/dwf.dll')
        self.dll = ct.CDLL(path)
        self.path = str(Path(path).resolve())
        self.handle = ct.c_int()
        pointer = ct.POINTER
        signatures = {
            'FDwfGetVersion': [ct.c_char_p], 'FDwfGetLastErrorMsg': [ct.c_char_p],
            'FDwfEnum': [ct.c_int, pointer(ct.c_int)],
            'FDwfEnumDeviceType': [ct.c_int, pointer(ct.c_int), pointer(ct.c_int)],
            'FDwfEnumDeviceName': [ct.c_int, ct.c_char_p], 'FDwfEnumSN': [ct.c_int, ct.c_char_p],
            'FDwfParamSet': [ct.c_int, ct.c_int],
            'FDwfDeviceOpen': [ct.c_int, pointer(ct.c_int)],
            'FDwfDeviceClose': [ct.c_int], 'FDwfDeviceAutoConfigureSet': [ct.c_int, ct.c_int],
            'FDwfAnalogInChannelEnableSet': [ct.c_int, ct.c_int, ct.c_int],
            'FDwfAnalogInChannelRangeSet': [ct.c_int, ct.c_int, ct.c_double],
            'FDwfAnalogInChannelRangeGet': [ct.c_int, ct.c_int, pointer(ct.c_double)],
            'FDwfAnalogInChannelOffsetSet': [ct.c_int, ct.c_int, ct.c_double],
            'FDwfAnalogInChannelFilterSet': [ct.c_int, ct.c_int, ct.c_int],
            'FDwfAnalogInChannelFilterGet': [ct.c_int, ct.c_int, pointer(ct.c_int)],
            'FDwfAnalogInFrequencySet': [ct.c_int, ct.c_double],
            'FDwfAnalogInFrequencyGet': [ct.c_int, pointer(ct.c_double)],
            'FDwfAnalogInAcquisitionModeSet': [ct.c_int, ct.c_int],
            'FDwfAnalogInRecordLengthSet': [ct.c_int, ct.c_double],
            'FDwfAnalogInTriggerSourceSet': [ct.c_int, ct.c_ubyte],
            'FDwfAnalogInConfigure': [ct.c_int, ct.c_int, ct.c_int],
            'FDwfAnalogInStatus': [ct.c_int, ct.c_int, pointer(ct.c_ubyte)],
            'FDwfAnalogInStatusRecord': [ct.c_int, pointer(ct.c_int), pointer(ct.c_int), pointer(ct.c_int)],
            'FDwfAnalogInStatusData2': [ct.c_int, ct.c_int, pointer(ct.c_double), ct.c_int, ct.c_int],
        }
        for name, args in signatures.items():
            func = getattr(self.dll, name)
            func.argtypes = args
            func.restype = ct.c_int
        version = ct.create_string_buffer(32)
        self.call('FDwfGetVersion', version)
        self.version = version.value.decode('ascii')
        if self.version != SDK_VERSION:
            raise AcquisitionError(f'WaveForms SDK {SDK_VERSION} required; found {self.version}')

    def call(self, name, *args):
        if not getattr(self.dll, name)(*args):
            error = ct.create_string_buffer(512)
            self.dll.FDwfGetLastErrorMsg(error)
            raise AcquisitionError(f'{name}: {error.value.decode(errors="replace")}')

    def devices(self):
        count = ct.c_int()
        self.call('FDwfEnum', 0, ct.byref(count))
        devices = []
        for index in range(count.value):
            kind, revision = ct.c_int(), ct.c_int()
            name, serial = ct.create_string_buffer(32), ct.create_string_buffer(32)
            self.call('FDwfEnumDeviceType', index, ct.byref(kind), ct.byref(revision))
            self.call('FDwfEnumDeviceName', index, name)
            self.call('FDwfEnumSN', index, serial)
            devices.append(dict(index=index, device_type=kind.value, revision=revision.value,
                                name=name.value.decode(), serial=serial.value.decode()))
        return devices

    def start(self, index, range_v, stop):
        devices = self.devices()
        device = next((d for d in devices if d['index'] == index), None)
        if device is None or device['device_type'] != 10:
            raise AcquisitionError('Select an Analog Discovery 3 (device type 10)')
        # Official SDK parameter: leave other instruments unchanged on close.
        self.call('FDwfParamSet', 4, 0)
        self.call('FDwfDeviceOpen', index, ct.byref(self.handle))
        h = self.handle
        self.call('FDwfDeviceAutoConfigureSet', h, 0)
        for channel in (0, 1):
            self.call('FDwfAnalogInChannelEnableSet', h, channel, 1)
            self.call('FDwfAnalogInChannelRangeSet', h, channel, 5.0 if channel == 0 else range_v)
            self.call('FDwfAnalogInChannelOffsetSet', h, channel, 0.0)
            self.call('FDwfAnalogInChannelFilterSet', h, channel, 1)  # filterAverage, as trial297
        self.call('FDwfAnalogInFrequencySet', h, float(SAMPLE_RATE))
        self.call('FDwfAnalogInAcquisitionModeSet', h, 3)  # acqmodeRecord
        self.call('FDwfAnalogInRecordLengthSet', h, -1.0)  # installed SDK sample: infinite
        self.call('FDwfAnalogInTriggerSourceSet', h, 0)  # trigsrcNone
        self.call('FDwfAnalogInConfigure', h, 1, 0)
        # With AutoConfigure=0, Get returns the applied hardware settings, not
        # the queued Set values. Validate only after Configure applies them.
        actual = ct.c_double()
        self.call('FDwfAnalogInFrequencyGet', h, ct.byref(actual))
        if abs(actual.value - SAMPLE_RATE) > .001:
            raise AcquisitionError(f'Unexpected sample rate {actual.value}')
        sample_rate = actual.value
        ranges = []
        for channel in (0, 1):
            averaging = ct.c_int()
            self.call('FDwfAnalogInChannelFilterGet', h, channel, ct.byref(averaging))
            if averaging.value != 1:
                raise AcquisitionError(f'AD3 averaging mode not applied: channel={channel + 1} actual={averaging.value}')
            self.call('FDwfAnalogInChannelRangeGet', h, channel, ct.byref(actual))
            ranges.append(actual.value)
        if stop.wait(2.0):
            raise AcquisitionError('Stopped during analog offset stabilization')
        start_wall, start_mono = time.time_ns(), time.monotonic_ns()
        self.call('FDwfAnalogInConfigure', h, 0, 1)
        end_mono, end_wall = time.monotonic_ns(), time.time_ns()
        sample_clock_reference = None
        if (0 <= end_mono - start_mono <= 1000000000 and
                abs((end_wall - start_wall) - (end_mono - start_mono)) <= 50000000):
            sample_clock_reference = dict(basis='PC_CONFIGURE_BRACKET', time_quality='ESTIMATED',
                pc_utc_before_ms=start_wall // 1000000, pc_utc_after_ms=end_wall // 1000000,
                hardware_start_accuracy='UNVERIFIED')
        return dict(device=device, sdk_version=self.version, sdk_library=self.path,
                    sample_rate_hz=sample_rate, channel_ranges_v=ranges, channel_offsets_v=[0, 0],
                    channel_sample_mode='AVERAGE', sample_clock_reference=sample_clock_reference)

    def blocks(self, stop):
        last_data = time.monotonic()
        while not stop.is_set():
            state = ct.c_ubyte()
            self.call('FDwfAnalogInStatus', self.handle, 1, ct.byref(state))
            available, lost, corrupt = ct.c_int(), ct.c_int(), ct.c_int()
            self.call('FDwfAnalogInStatusRecord', self.handle, ct.byref(available), ct.byref(lost), ct.byref(corrupt))
            if lost.value or corrupt.value:
                raise AcquisitionError(f'AD3_SAMPLE_LOSS lost={lost.value} corrupt={corrupt.value}')
            if not 0 <= available.value <= SAMPLE_RATE * 10:
                raise AcquisitionError('Invalid SDK available sample count')
            for offset in range(0, available.value, 5000):
                count = min(5000, available.value - offset)
                data = np.empty((count, 2), dtype='<f8')
                for channel in (0, 1):
                    values = np.empty(count, dtype=np.float64)
                    self.call('FDwfAnalogInStatusData2', self.handle, channel,
                              values.ctypes.data_as(ct.POINTER(ct.c_double)), offset, count)
                    data[:, channel] = values
                if not np.isfinite(data).all():
                    raise AcquisitionError('Nonfinite AD3 samples')
                last_data = time.monotonic()
                yield data
            if time.monotonic() - last_data > 5:
                raise AcquisitionError('AD3_NO_DATA_TIMEOUT')
            stop.wait(.005)

    def close(self):
        if self.handle.value:
            try:
                self.call('FDwfAnalogInConfigure', self.handle, 0, 0)
            finally:
                self.call('FDwfDeviceClose', self.handle)
                self.handle = ct.c_int()
