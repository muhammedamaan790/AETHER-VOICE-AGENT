"""Microphone selection.

Reproduced on this machine: PortAudio's default input is device 1, a headset mic that captures
effectively silence (RMS 1.5e-05 measured at the pipeline's own 16 kHz mono settings), while the
working mic is device 2. `AETHER_INPUT_DEVICE` existed only in the operator's head -- nothing in
the codebase read it -- so exporting it changed nothing and AETHER kept listening to a dead mic.

These tests pin the whole chain: env var -> resolved index -> the `device=` argument PortAudio
actually receives. A silent fallback is treated as a bug, not a convenience: it is indistinguishable
from a broken VAD.
"""

from __future__ import annotations

import numpy as np
import pytest
import sounddevice as sd

from aether.audio.vad import (
    InputDeviceError,
    MicVAD,
    describe_device,
    resolve_input_device,
)
from aether.trace import Trace


def a_real_input_device() -> int:
    """An index that genuinely has input channels on THIS machine.

    Hardcoding an index couples the suite to whatever was plugged in when it was written: these
    tests began failing the moment a headset was unplugged and device 2 became an output.
    """
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            return i
    pytest.skip("no input device on this machine")


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("AETHER_INPUT_DEVICE", raising=False)
    return monkeypatch


# --- resolution ------------------------------------------------------------------------

def test_no_env_preserves_the_portaudio_default(clean_env):
    """Unset must behave exactly as before: let PortAudio choose."""
    assert resolve_input_device() is None


def test_env_var_is_parsed_and_resolved(clean_env):
    dev = a_real_input_device()
    clean_env.setenv("AETHER_INPUT_DEVICE", str(dev))
    assert resolve_input_device() == dev


def test_env_var_tolerates_surrounding_whitespace(clean_env):
    dev = a_real_input_device()
    clean_env.setenv("AETHER_INPUT_DEVICE", f"  {dev} ")
    assert resolve_input_device() == dev


def test_empty_env_var_is_treated_as_unset(clean_env):
    clean_env.setenv("AETHER_INPUT_DEVICE", "   ")
    assert resolve_input_device() is None


def test_explicit_argument_beats_the_env_var(clean_env):
    """`--input-device` must stay authoritative over the environment."""
    clean_env.setenv("AETHER_INPUT_DEVICE", str(a_real_input_device()))
    assert resolve_input_device(1) == 1


# --- invalid configuration fails loudly --------------------------------------------------

def test_non_numeric_device_fails_clearly(clean_env):
    clean_env.setenv("AETHER_INPUT_DEVICE", "Microphone Array")
    with pytest.raises(InputDeviceError) as exc:
        resolve_input_device()
    msg = str(exc.value)
    assert "not an integer" in msg
    assert "query_devices" in msg, "the error should say how to list valid devices"


def test_nonexistent_device_fails_clearly(clean_env):
    clean_env.setenv("AETHER_INPUT_DEVICE", "9999")
    with pytest.raises(InputDeviceError) as exc:
        resolve_input_device()
    assert "does not exist" in str(exc.value)


def test_an_output_only_device_is_rejected(clean_env, monkeypatch):
    """Selecting a speaker as a microphone must be an error, not silent silence."""
    monkeypatch.setattr(sd, "query_devices",
                        lambda i: {"name": "Speakers", "max_input_channels": 0,
                                   "default_samplerate": 48000.0})
    clean_env.setenv("AETHER_INPUT_DEVICE", "3")
    with pytest.raises(InputDeviceError) as exc:
        resolve_input_device()
    assert "no input channels" in str(exc.value)


def test_a_bad_device_never_silently_falls_back(clean_env):
    """The whole point: a wrong device must stop, not quietly listen to the dead default."""
    clean_env.setenv("AETHER_INPUT_DEVICE", "9999")
    with pytest.raises(InputDeviceError):
        resolve_input_device()


# --- the resolved device actually reaches PortAudio ---------------------------------------

def capture_stream_kwargs(monkeypatch):
    seen = {}
    real = sd.InputStream

    class Spy(real):
        def __init__(self, *a, **k):
            seen.update(k)
            super().__init__(*a, **k)

    monkeypatch.setattr(sd, "InputStream", Spy)
    return seen


def test_env_device_is_passed_into_the_input_stream(clean_env, monkeypatch):
    """The bug was end-to-end, so this asserts end-to-end, not just the parse."""
    dev = a_real_input_device()
    clean_env.setenv("AETHER_INPUT_DEVICE", str(dev))
    seen = capture_stream_kwargs(monkeypatch)

    mic = MicVAD(Trace())

    assert seen["device"] == dev, "the resolved index must reach sounddevice"
    assert mic.device == dev


def test_default_passes_none_so_portaudio_chooses(clean_env, monkeypatch):
    seen = capture_stream_kwargs(monkeypatch)

    mic = MicVAD(Trace())

    assert seen["device"] is None, "unset must not change existing behaviour"
    assert mic.device is None


def test_explicit_device_argument_reaches_the_stream(clean_env, monkeypatch):
    seen = capture_stream_kwargs(monkeypatch)

    MicVAD(Trace(), device=1)

    assert seen["device"] == 1


# --- compatibility with the existing VAD/STT pipeline --------------------------------------

def test_stream_settings_stay_compatible_with_webrtcvad_and_whisper(clean_env, monkeypatch):
    """Device selection must not disturb the format the rest of the pipeline requires."""
    clean_env.setenv("AETHER_INPUT_DEVICE", str(a_real_input_device()))
    seen = capture_stream_kwargs(monkeypatch)

    mic = MicVAD(Trace())

    assert seen["samplerate"] == 16000, "webrtcvad needs 8/16/32/48k; faster-whisper wants 16k"
    assert seen["channels"] == 1, "mono"
    assert seen["dtype"] == "int16"
    assert seen["blocksize"] == mic.frame_samples == 320, "20 ms frames"


def test_detector_behaviour_is_unchanged_by_device_selection(clean_env, monkeypatch):
    """Selecting a device must not touch the VAD algorithm or the noise gate."""
    clean_env.setenv("AETHER_INPUT_DEVICE", str(a_real_input_device()))
    capture_stream_kwargs(monkeypatch)

    mic = MicVAD(Trace())

    assert mic.min_speech_ms == 250.0
    assert mic.noise_snr_margin == 3.0
    assert mic.onset_frames == 2 and mic.offset_frames == 25


# --- reporting -----------------------------------------------------------------------------

def test_describe_device_reports_the_resolved_endpoint_not_the_env_var(clean_env, monkeypatch):
    monkeypatch.setattr(sd, "query_devices",
                        lambda i: {"name": "Microphone Array (Intel)", "max_input_channels": 4,
                                   "default_samplerate": 44100.0})
    text = describe_device(2)
    assert text.startswith("[2] "), "the index actually used must be shown"
    assert "Microphone Array (Intel)" in text
    assert "44100 Hz" in text


def test_describe_device_resolves_none_to_the_actual_default(clean_env, monkeypatch):
    monkeypatch.setattr(sd, "default", type("d", (), {"device": [7, 3]})())
    monkeypatch.setattr(sd, "query_devices",
                        lambda i: {"name": f"dev{i}", "max_input_channels": 2,
                                   "default_samplerate": 16000.0})
    assert describe_device(None).startswith("[7] "), "None must report the real default index"


def test_describe_device_survives_a_missing_device(clean_env, monkeypatch):
    monkeypatch.setattr(sd, "query_devices", lambda i: (_ for _ in ()).throw(RuntimeError("gone")))
    assert "unavailable" in describe_device(42)


# --- no hidden hardcoding -------------------------------------------------------------------

def test_no_input_device_index_is_hardcoded_in_the_pipeline():
    """A hardcoded index would silently defeat the env var again."""
    import inspect

    from aether import spike
    from aether.audio import vad

    vad_src = inspect.getsource(vad.MicVAD.__init__)
    assert "device=device" in vad_src or "device = self.device" in vad_src

    spike_src = inspect.getsource(spike.Day1Spike.__init__)
    assert "device=input_device" in spike_src, "spike must forward, never pick, the input device"
    assert "device=1" not in spike_src and "device=2" not in spike_src


def test_spike_defaults_keep_input_device_unset():
    """`Day1Spike()` with no argument must defer to resolution, not pin a device."""
    import inspect

    from aether.spike import Day1Spike

    sig = inspect.signature(Day1Spike.__init__)
    assert sig.parameters["input_device"].default is None
