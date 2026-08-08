import threading
import time
from pathlib import Path

import pytest

from sparks.energy import (
    BUSY_GPU_WATTS,
    SOURCES_AGREE,
    SOURCES_DISAGREE,
    SOURCES_UNMEASURED,
    EnergyReading,
    Sampler,
    delta,
    watt_hours,
)


def a_reading(
    total_joules: float | None = 1000.0,
    gpu_nvml_joules: float | None = 300.0,
    gpu_firmware_joules: float | None = 367.0,
    idle_watts: float = 13.0,
    gpu_idle_watts: float = 3.8,
    seconds: float = 50.0,
) -> EnergyReading:
    return EnergyReading(
        total_joules=total_joules,
        gpu_nvml_joules=gpu_nvml_joules,
        gpu_firmware_joules=gpu_firmware_joules,
        idle_watts=idle_watts,
        gpu_idle_watts=gpu_idle_watts,
        seconds=seconds,
    )


def fake_chip(root: Path, index: int, name: str, **channels: tuple[str, str]) -> Path:
    chip = root / f"hwmon{index}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(f"{name}\n")
    for channel, (label, raw) in channels.items():
        (chip / f"{channel}_label").write_text(f"{label}\n")
        (chip / f"{channel}_input").write_text(f"{raw}\n")
    return chip


def spbm(root: Path) -> Path:
    return fake_chip(
        root,
        3,
        "spbm",
        power1=("soc_pkg", "12900000"),
        power2=("dc_input", "13070000"),
        power5=("sys_total", "13060000"),
        energy1=("pkg", "5000000000"),
        energy4=("gpu", "802860000"),
    )


def test_watt_hours_converts_from_joules() -> None:
    assert watt_hours(3600.0) == pytest.approx(1.0)
    assert watt_hours(0.0) == 0.0


def test_a_reading_reports_both_gpu_sources_separately() -> None:
    # NVML and the firmware counter disagree by a stable ~22.5% because they
    # measure at different boundaries. One unlabelled number is a trap: the
    # gpu/total ratio here is 0.30 by NVML and 0.37 by the firmware.
    r = a_reading()
    assert r.gpu_nvml_joules != r.gpu_firmware_joules
    assert r.marginal_joules == pytest.approx(1000.0 - 13.0 * 50.0)


def test_marginal_is_unknown_when_the_run_came_in_far_under_its_baseline() -> None:
    # Not 0.0: a run more than 10% under its own baseline means the neighbour
    # stopped mid-run, so the baseline describes a box that no longer exists.
    r = a_reading(total_joules=100.0, idle_watts=13.0, seconds=100.0)
    assert r.marginal_joules is None


def test_marginal_clamps_to_zero_just_under_the_baseline() -> None:
    # Within 10% under is measurement noise, and is reported as zero, not None.
    r = a_reading(total_joules=13.0 * 100.0 * 0.95, idle_watts=13.0, seconds=100.0)
    assert r.marginal_joules == 0.0


def test_marginal_is_unknown_with_no_baseline() -> None:
    # A sensorless box measured no idle power; marginal cannot be computed.
    r = a_reading(idle_watts=0.0)
    assert r.marginal_joules is None


def test_marginal_is_unknown_when_the_gpu_rail_was_already_busy() -> None:
    # The defining case: a neighbour's job puts the GPU rail in the tens of
    # watts during the baseline, so the baseline is theirs, and clamping the
    # marginal to 0.0 told users a contended run cost nothing.
    r = a_reading(gpu_idle_watts=BUSY_GPU_WATTS + 1.0)
    assert r.marginal_joules is None


def test_the_two_gpu_sources_are_cross_checked() -> None:
    # The measured ratio is ~1.22. A large departure means one counter reset,
    # which is the failure D2's backwards-delta guard cannot catch.
    ok = a_reading(gpu_nvml_joules=1000.0, gpu_firmware_joules=1220.0, seconds=10.0)
    assert ok.gpu_sources == SOURCES_AGREE
    bad = a_reading(gpu_nvml_joules=1000.0, gpu_firmware_joules=5000.0, seconds=10.0)
    assert bad.gpu_sources == SOURCES_DISAGREE


def test_the_tolerance_is_relative_not_absolute() -> None:
    # The ratio moves only ~2% across every regime, so a firmware reset that
    # shifts it 25% must be caught; an absolute +/-0.5 would not fire until 41%.
    drifted = a_reading(
        gpu_nvml_joules=1000.0, gpu_firmware_joules=1220.0 * 1.25, seconds=10.0
    )
    assert drifted.gpu_sources == SOURCES_DISAGREE


def test_nvml_absent_is_unmeasured_not_a_disagreement() -> None:
    # A driver without nvmlDeviceGetTotalEnergyConsumption reads 0.0 for NVML,
    # which the old boolean called a disagreement on every single run.
    r = a_reading(gpu_nvml_joules=0.0, gpu_firmware_joules=800.0, seconds=10.0)
    assert r.gpu_sources == SOURCES_UNMEASURED


def test_no_gpu_data_at_all_is_unmeasured_not_agreement() -> None:
    # Two zeros are not "the sources agree"; nothing was measured.
    r = a_reading(gpu_nvml_joules=0.0, gpu_firmware_joules=0.0, seconds=10.0)
    assert r.gpu_sources == SOURCES_UNMEASURED


def test_a_window_too_short_cannot_cross_check_the_sources() -> None:
    # Counter update granularity, not quantisation, breaks a short window: it
    # may catch one tick from one source and two from the other.
    r = a_reading(gpu_nvml_joules=1000.0, gpu_firmware_joules=1220.0, seconds=0.5)
    assert r.gpu_sources == SOURCES_UNMEASURED


def test_a_sampler_without_nvml_degrades_rather_than_raising() -> None:
    # Development happens on macOS, where there is no NVML and no hwmon.
    s = Sampler(nvml=None, hwmon=None)
    assert s.baseline(seconds=0.0).idle_watts == 0.0


def test_every_accessor_on_a_sensorless_box_reads_unknown() -> None:
    # None, not 0.0. A zero here is a measurement that says the box drew no
    # power, which is false; the honest answer is that nothing was measured.
    s = Sampler(nvml=None, hwmon=None)
    assert s.total_watts() is None
    assert s.total_joules() is None
    assert s.gpu_firmware_joules() is None
    assert s.gpu_nvml_joules() is None


def test_the_chip_is_found_by_name_among_its_neighbours(tmp_path: Path) -> None:
    # hwmon indices are probe order and move between boots, so a neighbour
    # holding a plausible-looking channel must not win.
    fake_chip(tmp_path, 0, "coretemp", power1=("sys_total", "999000000"))
    fake_chip(tmp_path, 1, "nvme", power1=("sys_total", "888000000"))
    spbm(tmp_path)
    assert Sampler.detect(root=tmp_path).total_watts() == pytest.approx(13.06)


def test_channels_are_resolved_by_label_not_by_index(tmp_path: Path) -> None:
    # `sys_total` is channel 5 on this box and the GPU energy counter is
    # `energy4`. Hardcoding `power1_input` would silently return `soc_pkg`,
    # which is a different measurement boundary that happens to look sane.
    chip = spbm(tmp_path)
    s = Sampler.detect(root=tmp_path)
    assert s.total_power == chip / "power5_input"
    assert s.gpu_energy == chip / "energy4_input"
    assert s.total_energy == chip / "energy1_input"


def test_power_is_microwatts_and_energy_is_microjoules(tmp_path: Path) -> None:
    # The kernel's hwmon ABI is micro for both, and node_exporter divides both
    # by 1e6. A divisor of 1000 would report the box's idle draw as 13 kW.
    spbm(tmp_path)
    s = Sampler.detect(root=tmp_path)
    assert s.total_watts() == pytest.approx(13.06)
    assert s.gpu_firmware_joules() == pytest.approx(802.86)
    assert s.total_joules() == pytest.approx(5000.0)


def test_whole_box_energy_falls_back_to_the_package_counter(tmp_path: Path) -> None:
    # This is the box's real case: 14 power channels but only 4 energy ones,
    # `pkg`, `cpu_e`, `cpu_p` and `gpu`, so there is no whole-box accumulator
    # to read. `pkg` is the closest available boundary.
    fake_chip(tmp_path, 0, "spbm", energy1=("pkg", "5000000000"))
    assert Sampler.detect(root=tmp_path).total_joules() == pytest.approx(5000.0)


def test_a_real_total_counter_wins_over_the_package_one(tmp_path: Path) -> None:
    # `pkg` is a substitute, not the intent, so firmware that grows the real
    # accumulator is picked up without a code change.
    fake_chip(
        tmp_path,
        0,
        "spbm",
        energy1=("pkg", "5000000000"),
        energy2=("sys_total", "6000000000"),
    )
    assert Sampler.detect(root=tmp_path).total_joules() == pytest.approx(6000.0)


def test_a_box_without_the_chip_reads_unknown_rather_than_raising(
    tmp_path: Path,
) -> None:
    fake_chip(tmp_path, 0, "coretemp", temp1=("Package id 0", "42000"))
    s = Sampler.detect(root=tmp_path)
    assert s.total_watts() is None
    assert s.gpu_firmware_joules() is None
    # The baseline stays a float: 0 W of idle is how "no baseline" is spelled
    # to the marginal subtraction, which then declines to subtract at all.
    assert s.baseline(seconds=60.0).idle_watts == 0.0


def test_a_missing_hwmon_directory_is_not_an_error(tmp_path: Path) -> None:
    # macOS, where /sys does not exist at all.
    s = Sampler.detect(root=tmp_path / "nothing here")
    assert s.hwmon is None
    assert s.total_watts() is None


def test_a_labelled_channel_with_no_input_file_is_skipped(tmp_path: Path) -> None:
    chip = fake_chip(tmp_path, 0, "spbm", power1=("sys_total", "13060000"))
    (chip / "power1_input").unlink()
    assert Sampler.detect(root=tmp_path).total_watts() is None


def test_an_unreadable_sensor_value_reads_unknown(tmp_path: Path) -> None:
    # A sensor that has gone away mid-run returns an error string, not a number.
    chip = spbm(tmp_path)
    (chip / "power5_input").write_text("N/A\n")
    assert Sampler.detect(root=tmp_path).total_watts() is None


def test_nvml_reports_millijoules(tmp_path: Path) -> None:
    # Measured on the box: 10024 mJ over 3 s is 3.34 W, matching PowerUsage.
    s = Sampler(nvml=lambda: 10024.0, hwmon=None)
    assert s.gpu_nvml_joules() == pytest.approx(10.024)


def test_an_nvml_call_that_fails_mid_run_reads_unknown() -> None:
    # A driver reload invalidates the handle, and a training run must survive it.
    def reloaded() -> float:
        raise RuntimeError("NVML_ERROR_UNINITIALIZED")

    assert Sampler(nvml=reloaded, hwmon=None).gpu_nvml_joules() is None


def test_an_nvml_counter_returning_a_non_finite_value_reads_unknown() -> None:
    # NaN parses and propagates silently all the way into json.dumps, where it
    # produces a token every strict parser rejects.
    assert Sampler(nvml=lambda: float("nan"), hwmon=None).gpu_nvml_joules() is None


def test_the_gauge_baseline_reads_more_than_once(tmp_path: Path) -> None:
    # A power-only chip has no energy accumulator, so the baseline falls back to
    # integrating the power gauge at 1 Hz.
    chip = fake_chip(tmp_path, 0, "spbm", power1=("sys_total", "10000000"))
    sensor = chip / "power1_input"

    def step_up() -> None:
        time.sleep(0.5)
        sensor.write_text("20000000\n")

    writer = threading.Thread(target=step_up)
    writer.start()
    try:
        watts = Sampler.detect(root=tmp_path).baseline(seconds=1.2).idle_watts
    finally:
        writer.join()
    # Samples land at t=0 and t=1.0, either side of the step at t=0.5, so a
    # value strictly between the two is proof it read more than once.
    assert 10.0 < watts < 20.0


def test_the_gauge_baseline_uses_the_median_so_a_sentinel_cannot_skew_it(
    tmp_path: Path,
) -> None:
    # A dead sensor reports the u32 sentinel; the mean of many good samples and
    # one sentinel is wildly wrong, the median is not, and the sentinel is
    # dropped outright before it even reaches the median.
    chip = fake_chip(tmp_path, 0, "spbm", power1=("sys_total", "13060000"))
    sensor = chip / "power1_input"

    def spike() -> None:
        time.sleep(0.4)
        sensor.write_text("4294967295\n")  # the u32 sentinel, ~4295 W

    writer = threading.Thread(target=spike)
    writer.start()
    try:
        watts = Sampler.detect(root=tmp_path).baseline(seconds=1.2).idle_watts
    finally:
        writer.join()
    assert watts == pytest.approx(13.06)


def test_the_counter_baseline_is_the_delta_over_the_window(tmp_path: Path) -> None:
    # With an energy accumulator, the baseline is a counter delta divided by its
    # window, which is the exact average power. Advance the counter mid-window.
    chip = spbm(tmp_path)
    box = chip / "energy1_input"
    gpu = chip / "energy4_input"

    def burn() -> None:
        time.sleep(0.3)
        box.write_text("5000013000\n")  # +13000 uJ over the start value
        gpu.write_text("802864000\n")  # +4000 uJ

    writer = threading.Thread(target=burn)
    writer.start()
    try:
        base = Sampler.detect(root=tmp_path).baseline(seconds=0.6)
    finally:
        writer.join()
    # 13000 uJ = 0.013 J over 0.6 s ~ 0.0217 W; 4000 uJ ~ 0.0067 W.
    assert base.idle_watts == pytest.approx(0.013 / 0.6)
    assert base.gpu_watts == pytest.approx(0.004 / 0.6)


def test_a_zero_length_baseline_reads_zero_rather_than_dividing_by_zero(
    tmp_path: Path,
) -> None:
    spbm(tmp_path)
    base = Sampler.detect(root=tmp_path).baseline(0.0)
    assert base.idle_watts == 0.0
    assert base.gpu_watts == 0.0


def test_an_implausible_power_reading_is_dropped(tmp_path: Path) -> None:
    # The u32 sentinel a dead sensor reports is not a 4295 W draw.
    chip = spbm(tmp_path)
    (chip / "power5_input").write_text("4294967295\n")
    assert Sampler.detect(root=tmp_path).total_watts() is None


def test_a_five_kilowatt_draw_is_a_real_reading_not_a_sentinel(tmp_path: Path) -> None:
    # The old guard was a 4000 W ceiling, which is under what a large multi-GPU
    # node genuinely draws, so a real reading was thrown away as implausible.
    # Only the exact u32 sentinel means "dead sensor"; every finite value below
    # it is a measurement, however large.
    chip = spbm(tmp_path)
    (chip / "power5_input").write_text("5000000000\n")  # 5000 W, a real draw
    assert Sampler.detect(root=tmp_path).total_watts() == pytest.approx(5000.0)


def test_a_glitched_start_read_makes_the_baseline_unknown_not_enormous(
    tmp_path: Path,
) -> None:
    # The hazard behind coalescing a failed read to 0.0: if the START of a
    # counter delta reads as zero, the delta becomes the whole accumulator. Here
    # that would be 5000013000 uJ over 0.6 s, reporting ~8.3 MW of idle draw and
    # making every marginal figure derived from it garbage. An endpoint that
    # could not be read means the delta is unknown, so the baseline is absent.
    chip = spbm(tmp_path)
    box = chip / "energy1_input"
    box.write_text("N/A\n")  # unreadable at the start of the window

    def recover() -> None:
        time.sleep(0.3)
        box.write_text("5000013000\n")

    writer = threading.Thread(target=recover)
    writer.start()
    try:
        base = Sampler.detect(root=tmp_path).baseline(seconds=0.6)
    finally:
        writer.join()
    assert base.idle_watts == 0.0


def test_marginal_is_unknown_when_the_total_was_never_measured() -> None:
    # A sensorless box used to record total_joules=0.0, which reads as "this run
    # drew nothing" rather than "nothing was measured". With the total unknown
    # there is no subtraction to do.
    assert a_reading(total_joules=None).marginal_joules is None


def test_a_gpu_source_that_was_not_read_is_unmeasured_not_disagreement() -> None:
    assert a_reading(gpu_nvml_joules=None).gpu_sources == SOURCES_UNMEASURED
    assert a_reading(gpu_firmware_joules=None).gpu_sources == SOURCES_UNMEASURED


def test_every_ratio_ever_measured_on_real_hardware_reads_as_agreeing() -> None:
    # 1.198, 1.225, 1.223 and 1.195 are the four regimes observed on the box.
    # A tolerance too tight calls a healthy run a counter reset.
    for ratio in (1.198, 1.225, 1.223, 1.195):
        reading = a_reading(gpu_nvml_joules=300.0, gpu_firmware_joules=300.0 * ratio)
        assert reading.gpu_sources == SOURCES_AGREE, ratio


def test_a_counter_that_reset_mid_run_is_caught_while_the_run_is_young() -> None:
    # A reset leaves the two sources far apart. A tolerance wide enough to miss
    # this reports a bad energy figure as trustworthy.
    reading = a_reading(gpu_nvml_joules=300.0, gpu_firmware_joules=300.0 * 1.8)
    assert reading.gpu_sources == SOURCES_DISAGREE


def test_a_gpu_rail_already_under_load_yields_no_marginal_energy() -> None:
    # Absolute watts, not BUSY_GPU_WATTS +/- 1: a test written against the
    # constant moves with it and pins nothing. 3.8 W is the measured idle rail
    # on this hardware; 20 W is somebody else's job already running.
    assert a_reading(gpu_idle_watts=20.0).marginal_joules is None
    assert a_reading(gpu_idle_watts=3.8).marginal_joules is not None


def test_a_window_too_short_to_cross_check_says_so_rather_than_guessing() -> None:
    # Absolute seconds, not the constant: below the floor the two counters may
    # catch a different number of ticks, so their ratio means nothing.
    assert a_reading(seconds=0.5).gpu_sources == SOURCES_UNMEASURED
    # 3s must still be long enough, or a short run loses its cross-check.
    assert a_reading(seconds=3.0).gpu_sources == SOURCES_AGREE


def test_a_delta_needs_both_endpoints_and_refuses_to_run_backwards() -> None:
    # A missing endpoint coalesced to 0.0 once reported the whole accumulator
    # as the run's energy; a backwards delta means the counter reset mid-run.
    # Both are non-measurements, and None is how the rest of the code hears it.
    assert delta(100.0, 250.0) == 150.0
    assert delta(None, 250.0) is None
    assert delta(100.0, None) is None
    assert delta(250.0, 100.0) is None
