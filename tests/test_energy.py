import threading
import time
from pathlib import Path

import pytest

from sparks.energy import EnergyReading, Sampler, watt_hours


def fake_chip(root: Path, index: int, name: str, **channels: tuple[str, str]) -> Path:
    """A hwmon chip directory of the shape the kernel presents: a `name`, and
    channels whose meaning lives only in their `*_label` file.

    Channels are given as `power3=("sys_total", "13060000")`, raw sysfs values
    in micro units."""
    chip = root / f"hwmon{index}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(f"{name}\n")
    for channel, (label, raw) in channels.items():
        (chip / f"{channel}_label").write_text(f"{label}\n")
        (chip / f"{channel}_input").write_text(f"{raw}\n")
    return chip


def spbm(root: Path) -> Path:
    """The box's chip as measured: `sys_total` is not channel 1, the GPU energy
    counter is `energy4`, and there is no `sys_total` energy accumulator."""
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
    r = EnergyReading(
        total_joules=1000.0,
        gpu_nvml_joules=300.0,
        gpu_firmware_joules=367.0,
        idle_watts=13.0,
        seconds=50.0,
    )
    assert r.gpu_nvml_joules != r.gpu_firmware_joules
    assert r.marginal_joules == pytest.approx(1000.0 - 13.0 * 50.0)


def test_marginal_energy_never_goes_negative() -> None:
    # A run quieter than the baseline is measurement noise, not free energy.
    r = EnergyReading(
        total_joules=100.0,
        gpu_nvml_joules=0.0,
        gpu_firmware_joules=0.0,
        idle_watts=13.0,
        seconds=100.0,
    )
    assert r.marginal_joules == 0.0


def test_the_two_gpu_sources_are_cross_checked() -> None:
    # The measured ratio is ~1.22. A large departure means one counter reset,
    # which is the failure D2's backwards-delta guard cannot catch.
    ok = EnergyReading(
        total_joules=1.0,
        gpu_nvml_joules=1000.0,
        gpu_firmware_joules=1220.0,
        idle_watts=0.0,
        seconds=1.0,
    )
    assert ok.sources_agree
    bad = EnergyReading(
        total_joules=1.0,
        gpu_nvml_joules=1000.0,
        gpu_firmware_joules=5000.0,
        idle_watts=0.0,
        seconds=1.0,
    )
    assert not bad.sources_agree


def test_a_reading_with_no_gpu_data_at_all_is_not_a_disagreement() -> None:
    # A box without the sensors reports zeros, and two zeros are consistent.
    r = EnergyReading(
        total_joules=0.0,
        gpu_nvml_joules=0.0,
        gpu_firmware_joules=0.0,
        idle_watts=0.0,
        seconds=1.0,
    )
    assert r.sources_agree


def test_a_sampler_without_nvml_degrades_rather_than_raising() -> None:
    # Development happens on macOS, where there is no NVML and no hwmon.
    s = Sampler(nvml=None, hwmon=None)
    assert s.baseline_watts(seconds=0.0) == 0.0


def test_every_accessor_on_a_sensorless_box_reads_zero() -> None:
    s = Sampler(nvml=None, hwmon=None)
    assert s.total_watts() == 0.0
    assert s.total_joules() == 0.0
    assert s.gpu_firmware_joules() == 0.0
    assert s.gpu_nvml_joules() == 0.0


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


def test_a_box_without_the_chip_reads_zero_rather_than_raising(tmp_path: Path) -> None:
    fake_chip(tmp_path, 0, "coretemp", temp1=("Package id 0", "42000"))
    s = Sampler.detect(root=tmp_path)
    assert s.total_watts() == 0.0
    assert s.gpu_firmware_joules() == 0.0
    assert s.baseline_watts(seconds=60.0) == 0.0


def test_a_missing_hwmon_directory_is_not_an_error(tmp_path: Path) -> None:
    # macOS, where /sys does not exist at all.
    s = Sampler.detect(root=tmp_path / "nothing here")
    assert s.hwmon is None
    assert s.total_watts() == 0.0


def test_a_labelled_channel_with_no_input_file_is_skipped(tmp_path: Path) -> None:
    chip = fake_chip(tmp_path, 0, "spbm", power1=("sys_total", "13060000"))
    (chip / "power1_input").unlink()
    assert Sampler.detect(root=tmp_path).total_watts() == 0.0


def test_an_unreadable_sensor_value_reads_zero(tmp_path: Path) -> None:
    # A sensor that has gone away mid-run returns an error string, not a number.
    chip = spbm(tmp_path)
    (chip / "power5_input").write_text("N/A\n")
    assert Sampler.detect(root=tmp_path).total_watts() == 0.0


def test_nvml_reports_millijoules(tmp_path: Path) -> None:
    # Measured on the box: 10024 mJ over 3 s is 3.34 W, matching PowerUsage.
    s = Sampler(nvml=lambda: 10024.0, hwmon=None)
    assert s.gpu_nvml_joules() == pytest.approx(10.024)


def test_an_nvml_call_that_fails_mid_run_reads_zero() -> None:
    # A driver reload invalidates the handle, and a training run must survive it.
    def reloaded() -> float:
        raise RuntimeError("NVML_ERROR_UNINITIALIZED")

    assert Sampler(nvml=reloaded, hwmon=None).gpu_nvml_joules() == 0.0


def test_the_baseline_is_the_mean_of_the_samples(tmp_path: Path) -> None:
    chip = fake_chip(tmp_path, 0, "spbm", power1=("sys_total", "10000000"))
    sensor = chip / "power1_input"

    def step_up() -> None:
        time.sleep(0.5)
        sensor.write_text("20000000\n")

    writer = threading.Thread(target=step_up)
    writer.start()
    try:
        watts = Sampler.detect(root=tmp_path).baseline_watts(seconds=1.2)
    finally:
        writer.join()
    # Samples land at t=0 and t=1.0, either side of the step at t=0.5, so a
    # mean strictly between the two is proof it read more than once.
    assert 10.0 < watts < 20.0


def test_a_baseline_of_one_sample_is_that_sample(tmp_path: Path) -> None:
    spbm(tmp_path)
    assert Sampler.detect(root=tmp_path).baseline_watts(0.1) == pytest.approx(13.06)


def test_a_zero_length_baseline_reads_zero_rather_than_dividing_by_zero(
    tmp_path: Path,
) -> None:
    spbm(tmp_path)
    assert Sampler.detect(root=tmp_path).baseline_watts(0.0) == 0.0
