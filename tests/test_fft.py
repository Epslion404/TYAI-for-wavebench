from __future__ import annotations

import numpy as np
import pytest

from wavebench.data.fft import analyze_fft


def test_even_sample_nyquist_tone_has_unit_amplitude() -> None:
    samples = 16
    sample_rate_hz = 16.0
    time_s = np.arange(samples) / sample_rate_hz
    waveform = np.column_stack((time_s, (-1.0) ** np.arange(samples)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == 8.0
    assert result["peak_amplitude_v"] == pytest.approx(1.0)


def test_odd_sample_paired_bin_keeps_one_sided_amplitude() -> None:
    samples = 15
    sample_rate_hz = 15.0
    time_s = np.arange(samples) / sample_rate_hz
    waveform = np.column_stack((time_s, np.cos(2.0 * np.pi * 6.0 * time_s)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == pytest.approx(6.0)
    assert result["peak_amplitude_v"] == pytest.approx(1.0, rel=0.01)
