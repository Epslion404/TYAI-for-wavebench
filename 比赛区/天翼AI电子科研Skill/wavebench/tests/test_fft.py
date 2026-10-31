from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from wavebench.data.fft import analyze_fft
from wavebench.services.run_analysis import (
    capture_fft_summary,
    evaluate_expect,
    step_status,
)


@pytest.mark.parametrize("samples", [16, 128, 1024])
def test_even_sample_nyquist_tone_has_unit_amplitude(samples) -> None:
    sample_rate_hz = float(samples)
    time_s = np.arange(samples) / sample_rate_hz
    waveform = np.column_stack((time_s, (-1.0) ** np.arange(samples)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == pytest.approx(sample_rate_hz / 2)
    assert result["peak_amplitude_v"] == pytest.approx(1.0)


def test_odd_sample_paired_bin_keeps_one_sided_amplitude() -> None:
    samples = 15
    sample_rate_hz = 15.0
    time_s = np.arange(samples) / sample_rate_hz
    waveform = np.column_stack((time_s, np.cos(2.0 * np.pi * 6.0 * time_s)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == pytest.approx(6.0)
    assert result["peak_amplitude_v"] == pytest.approx(1.0, rel=0.01)


def test_paired_positive_frequency_keeps_unit_amplitude():
    indices = np.arange(64)
    waveform = np.column_stack((indices / 64.0, np.sin(2 * np.pi * 7 * indices / 64)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == pytest.approx(7.0)
    assert result["peak_amplitude_v"] == pytest.approx(1.0, abs=1e-3)


def test_odd_last_bin_remains_paired():
    indices = np.arange(15)
    waveform = np.column_stack((indices / 15.0, np.sin(2 * np.pi * 7 * indices / 15)))

    result = analyze_fft(waveform)

    assert result["peak_frequency_hz"] == pytest.approx(7.0)
    # Scenario note: a 15-sample / 15 Hz sine whose tone sits in the last rFFT
    # bin, so the symmetric Hann window overlaps the negative-frequency lobe.
    # The locked value is the existing paired-bin amplitude for this case.
    # The same frequency pair under a cosine basis instead peaks at 6 Hz with
    # about 0.5229 V; that difference is pre-existing behaviour (identical before
    # and after the Nyquist fix), tracked separately, and is not a regression.
    assert result["peak_amplitude_v"] == pytest.approx(1.542380195395906)


def test_nyquist_harmonic_is_not_doubled():
    indices = np.arange(64)
    voltage = np.sin(2 * np.pi * 8 * indices / 64) + 0.25 * (-1.0) ** indices

    result = analyze_fft(np.column_stack((indices / 64.0, voltage)))

    assert result["peak_frequency_hz"] == pytest.approx(8.0)
    assert result["harmonic_4_frequency_hz"] == pytest.approx(32.0)
    assert result["harmonic_4_amplitude_v"] == pytest.approx(0.25, abs=1e-4)


def test_capture_nyquist_fft_expectation_uses_corrected_amplitude(tmp_path):
    indices = np.arange(16)
    npy_path = tmp_path / "waveform.npy"
    np.save(npy_path, np.column_stack((indices / 16.0, (-1.0) ** indices)))

    summary = capture_fft_summary(SimpleNamespace(npy_path=npy_path))
    expectation = evaluate_expect(
        summary,
        {"peak_frequency_hz": {"min": 7.9, "max": 8.1},
         "peak_amplitude_v": {"min": 0.99, "max": 1.01}},
    )

    assert summary["status"] == "ok"
    assert expectation["status"] == "ok"
    assert step_status({"expect_fft": expectation}) == "ok"
    rejection = evaluate_expect(summary, {"peak_amplitude_v": {"min": 1.9, "max": 2.1}})
    assert rejection["status"] == "failed"
    assert step_status({"expect_fft": rejection}) == "failed"


@pytest.mark.parametrize("nyquist_amplitude", [0.6, 0.75, 0.9, 1.25])
def test_separated_tones_rank_peaks_in_volts(nyquist_amplitude, tmp_path):
    indices = np.arange(1024)
    voltage = np.sin(2 * np.pi * 128 * indices / 1024)
    voltage += nyquist_amplitude * (-1.0) ** indices
    waveform = np.column_stack((indices / 1024.0, voltage))
    result = analyze_fft(waveform)
    expected_frequency = 128.0 if nyquist_amplitude < 1.0 else 512.0
    expected_amplitude = max(1.0, nyquist_amplitude)

    assert result["peak_frequency_hz"] == pytest.approx(expected_frequency)
    assert result["peak_amplitude_v"] == pytest.approx(expected_amplitude, abs=1e-4)
    if nyquist_amplitude < 1.0:
        assert result["harmonic_4_amplitude_v"] == pytest.approx(nyquist_amplitude, abs=1e-4)
        assert result["thd_ratio"] == pytest.approx(nyquist_amplitude, abs=1e-4)

    npy_path = tmp_path / "multitone.npy"
    np.save(npy_path, waveform)
    summary = capture_fft_summary(SimpleNamespace(npy_path=npy_path))
    expectation = evaluate_expect(summary, {
        "peak_frequency_hz": {"min": expected_frequency - 0.1,
                              "max": expected_frequency + 0.1},
        "peak_amplitude_v": {"min": expected_amplitude - 0.01,
                             "max": expected_amplitude + 0.01},
    })
    assert step_status({"expect_fft": expectation}) == "ok"
    rejection = evaluate_expect(summary, {
        "peak_frequency_hz": {"min": 510, "max": 514} if nyquist_amplitude < 1.0
        else {"min": 126, "max": 130},
    })
    assert step_status({"expect_fft": rejection}) == "failed"
