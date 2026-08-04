"""What a run cost, measured two ways because the two disagree.

NVML's `nvmlDeviceGetTotalEnergyConsumption` and the firmware's `gpu` energy
counter differ by a stable 22.5% under load, reproducible to three significant
figures. That is a measurement-boundary difference, not noise, so a single
"GPU energy" number is meaningless without saying which one it is. Both are
reported, and their ratio is the cross-check that catches a counter reset.

Whole-box energy has no counter of its own, only the `sys_total` gauge, so
`total_joules` reads the `pkg` accumulator instead: the nearest boundary the
firmware actually counts. Integrating the gauge for the whole run was the
alternative and is worse, because the sampling loop that would do it costs
~0.8 W and so charges the run for the cost of measuring it.

The idle baseline is the one thing sampled rather than counted, at 1 Hz and
in-process rather than read back from Prometheus: at a 60 s window the 15 s
scrape integral was measured up to 7% wrong.

Everything here reads sysfs directly and every accessor degrades to 0.0 rather
than raising. This package is developed on macOS, where none of these paths
exist, and `spbm_enabled` is false on boxes that did not opt in. A missing
sensor is a degraded reading; it is never a reason for a training run to fail.
"""

import importlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Self

HWMON = Path("/sys/class/hwmon")
CHIP = "spbm"
"""The board power monitor's `name`. hwmon indices are assigned in probe order
and move between boots, so the chip is found by name, never as `hwmon3`."""

MICRO = 1_000_000.0
"""hwmon reports power in microwatts and energy in microjoules, per the kernel's
sysfs-interface ABI, and node_exporter divides both by the same 1e6 to produce
`node_hwmon_power_watt`. The box's 13 W idle reads 13060000 in `power1_input`;
a divisor of 1000 would report 13 kW."""

SAMPLE_INTERVAL = 1.0

TOTAL_POWER_LABELS = ("sys_total",)
"""The whole box, DC side. It excludes PSU conversion loss, so it reads under a
wall-socket meter and must not be presented as one."""

TOTAL_ENERGY_LABELS = ("sys_total", "pkg")
"""Whole-box energy, best available. There is no `sys_total` accumulator today:
the firmware exposes 14 power channels but only 4 energy counters, `pkg`,
`cpu_e`, `cpu_p` and `gpu`. `pkg` is the closest, and on this box `sys_total`
tracks it to within noise at idle. The preference order means a firmware that
later adds the real thing is picked up without a code change."""

GPU_ENERGY_LABELS = ("gpu",)
"""The GPU rail, measured at the regulator input rather than at the GPU domain,
which is why it reads ~22.5% above NVML."""

SOURCE_RATIO = 1.22
"""Measured firmware/NVML ratio for GPU energy. Used only as a sanity bound."""
RATIO_TOLERANCE = 0.5


def watt_hours(joules: float) -> float:
    return joules / 3600.0


@dataclass(frozen=True)
class EnergyReading:
    total_joules: float
    gpu_nvml_joules: float
    gpu_firmware_joules: float
    idle_watts: float
    seconds: float

    @property
    def marginal_joules(self) -> float:
        """Energy attributable to the run: total minus what the box would have
        drawn anyway. Clamped at zero, because a run quieter than its own
        baseline is measurement noise rather than free energy."""
        return max(0.0, self.total_joules - self.idle_watts * self.seconds)

    @property
    def sources_agree(self) -> bool:
        """Whether the two GPU counters are in their usual relationship.

        False means one of them reset mid-run. This catches the case D2's
        backwards-delta guard misses: a driver reload that re-accumulates past
        the start value gives a wrong but positive delta."""
        if self.gpu_nvml_joules <= 0:
            return self.gpu_firmware_joules <= 0
        ratio = self.gpu_firmware_joules / self.gpu_nvml_joules
        return abs(ratio - SOURCE_RATIO) <= RATIO_TOLERANCE


class Sampler:
    """Reads the box's energy sensors, or reports zeros where there are none."""

    def __init__(
        self,
        nvml: Callable[[], float] | None = None,
        hwmon: Path | None = None,
    ) -> None:
        """`nvml` reads NVML's counter in millijoules; `hwmon` is the chip
        directory. Either may be None, which makes its readings 0.0."""
        self.nvml = nvml
        self.hwmon = hwmon
        self.total_power = _channel(hwmon, "power", TOTAL_POWER_LABELS)
        self.total_energy = _channel(hwmon, "energy", TOTAL_ENERGY_LABELS)
        self.gpu_energy = _channel(hwmon, "energy", GPU_ENERGY_LABELS)

    @classmethod
    def detect(cls, root: Path = HWMON) -> Self:
        """A sampler wired to whatever this box actually offers."""
        return cls(nvml=_nvml_counter(), hwmon=_spbm_chip(root))

    def total_watts(self) -> float:
        """Instantaneous whole-box draw."""
        return _read_micro(self.total_power)

    def total_joules(self) -> float:
        """Whole-box energy counter. Read as a delta across the run."""
        return _read_micro(self.total_energy)

    def gpu_firmware_joules(self) -> float:
        """The GPU rail's energy counter. Read as a delta across the run."""
        return _read_micro(self.gpu_energy)

    def gpu_nvml_joules(self) -> float:
        """The GPU domain's energy counter, which NVML reports in millijoules.

        It resets on driver reload, which is why `EnergyReading` cross-checks it
        against the firmware counter rather than trusting a positive delta."""
        if self.nvml is None:
            return 0.0
        try:
            return self.nvml() / 1000.0
        except Exception:  # NVML's own error type, and a reload invalidates the
            return 0.0  # handle mid-run; either way the reading is degraded.

    def baseline_watts(self, seconds: float) -> float:
        """Mean whole-box draw over `seconds`, sampled at 1 Hz.

        Sampled in-process rather than read back from Prometheus because at a
        60 s window the 15 s scrape integral was measured up to 7% wrong:
        `sys_total` carries ~1.9 W of jitter and only 4-5 scrapes land in the
        window.

        The loop costs about 0.8 W while it runs, which is 6% of the idle
        figure it is measuring, so call it before doing anything else expensive
        and never during the run it is the baseline for."""
        if self.total_power is None:
            return 0.0
        samples: list[float] = []
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            samples.append(_read_micro(self.total_power))
            time.sleep(min(SAMPLE_INTERVAL, remaining))
        return fmean(samples) if samples else 0.0


def _spbm_chip(root: Path = HWMON) -> Path | None:
    """The board power monitor's chip directory, by `name`."""
    try:
        chips = sorted(root.glob("hwmon*"))
    except OSError:
        return None
    return next((c for c in chips if _read(c / "name") == CHIP), None)


def _channel(chip: Path | None, kind: str, labels: Sequence[str]) -> Path | None:
    """The `<kind>N_input` file behind the first of `labels` this chip offers.

    Channel numbers are firmware ordering and mean nothing, so a channel is
    resolved through its `<kind>N_label` file. This is the same join every
    hwmon PromQL query needs, for the same reason."""
    if chip is None:
        return None
    inputs: dict[str, Path] = {}
    for label_file in sorted(chip.glob(f"{kind}*_label")):
        reading = label_file.with_name(
            label_file.name.removesuffix("_label") + "_input"
        )
        label = _read(label_file)
        if label is not None and reading.exists():
            inputs.setdefault(label, reading)
    return next((inputs[label] for label in labels if label in inputs), None)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_micro(path: Path | None) -> float:
    """A sysfs sensor in base units, or 0.0 if it is missing or unparseable."""
    if path is None:
        return 0.0
    raw = _read(path)
    if raw is None:
        return 0.0
    try:
        return float(raw) / MICRO
    except ValueError:
        return 0.0


def _nvml_counter() -> Callable[[], float] | None:
    """Reads NVML's total energy counter in millijoules, or None where NVML is
    not available.

    Imported by name because nvidia-ml-py is absent on every machine this
    package is developed on, and importing this module must not depend on it."""
    try:
        nvml = importlib.import_module("pynvml")
        nvml.nvmlInit()
        device = nvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:  # NVML raises its own error type, which needs the import
        return None

    def counter() -> float:
        return float(nvml.nvmlDeviceGetTotalEnergyConsumption(device))

    return counter
