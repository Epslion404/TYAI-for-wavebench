"""Amplitude regression tests for the FFT used by capture inspect and expect_fft."""

from unittest.mock import patch

import numpy as np
import pytest

from wavebench.data.fft import analyze_fft


@pytest.mark.parametrize("samples", [128, 1024])
def test_even_nyquist_amplitude_has_no_duplicate_partner(samples):
    time = np.arange(samples) / 1024
    signal = 1.25 * (-1.0) ** np.arange(samples)
    # A rectangular window isolates scaling from Hann edge-bin leakage.
    with patch("wavebench.data.fft.np.hanning", side_effect=np.ones):
        result = analyze_fft(np.column_stack((time, signal)))
    assert result["peak_frequency_hz"] == pytest.approx(512)
    assert result["peak_amplitude_v"] == pytest.approx(1.25)


def test_odd_last_bin_retains_single_sided_scaling():
    samples = 129
    time = np.arange(samples) / samples
    signal = 1.25 * np.cos(2 * np.pi * (samples // 2) * time)
    with patch("wavebench.data.fft.np.hanning", side_effect=np.ones):
        result = analyze_fft(np.column_stack((time, signal)))
    assert result["peak_frequency_hz"] == pytest.approx(samples // 2)
    assert result["peak_amplitude_v"] == pytest.approx(1.25)


@pytest.mark.parametrize("samples", [1023, 1024])
def test_hann_interior_tone_and_offset_keep_calibrated_amplitude(samples):
    time = np.arange(samples) / samples
    signal = 4 + 1.25 * np.cos(2 * np.pi * 64 * time)
    result = analyze_fft(np.column_stack((time, signal)))
    assert result["peak_frequency_hz"] == pytest.approx(64)
    assert result["peak_amplitude_v"] == pytest.approx(1.25, abs=0.0001)


def test_hann_nyquist_harmonic_amplitude_is_not_doubled():
    samples = 1024
    time = np.arange(samples) / samples
    signal = 3 * np.cos(2 * np.pi * 128 * time) + 0.5 * (-1.0) ** np.arange(samples)
    result = analyze_fft(np.column_stack((time, signal)), max_harmonic_order=4)
    assert result["peak_frequency_hz"] == pytest.approx(128)
    assert result["harmonic_4_frequency_hz"] == pytest.approx(512)
    assert result["harmonic_4_amplitude_v"] == pytest.approx(0.5, abs=0.0001)


@pytest.mark.parametrize("samples", [16, 128, 1024])
def test_actual_hann_nyquist_peak_is_not_promoted_to_leakage_bin(samples):
    time = np.arange(samples) / samples
    result = analyze_fft(np.column_stack((time, (-1.0) ** np.arange(samples))))
    assert result["peak_frequency_hz"] == pytest.approx(samples / 2)
    assert result["peak_amplitude_v"] == pytest.approx(1.0)
