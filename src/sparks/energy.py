"""What a run cost: the box's hwmon power and energy channels, NVML's GPU
counter, and the idle baseline they are measured against. Every accessor
degrades to None rather than raising."""

import functools
import importlib
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Self

HWMON = Path("/sys/class/hwmon")
CHIP = "spbm"
MICRO = 1_000_000.0
SAMPLE_INTERVAL = 1.0

TOTAL_POWER_LABELS = ("sys_total",)
TOTAL_ENERGY_LABELS = ("sys_total", "pkg")
GPU_ENERGY_LABELS = ("gpu",)

SOURCE_RATIO = 1.22
RATIO_TOLERANCE = 0.15
"""Relative to SOURCE_RATIO, never absolute: the whole observed spread is 2.1%."""

BUSY_GPU_WATTS = 10.0
UNDER_BASELINE_FRACTION = 0.9
MIN_COUNTER_WINDOW_SECONDS = 2.0
U32_SENTINEL = 4294967295.0

SOURCES_AGREE = "agree"
SOURCES_DISAGREE = "disagree"
SOURCES_UNMEASURED = "unmeasured"


def watt_hours(joules: float) -> float:
    return joules / 3600.0


@dataclass(frozen=True)
class Baseline:
    idle_watts: float
    gpu_watts: float


@dataclass(frozen=True)
class EnergyReading:
    """What the run drew. None means unmeasured, and never 0.0."""

    total_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    gpu_idle_watts: float
    seconds: float

    @property
    def marginal_joules(self) -> float | None:
        if self.total_joules is None:
            return None
        baseline_joules = self._baseline_joules()
        if baseline_joules is None:
            return None
        if self.total_joules < baseline_joules * UNDER_BASELINE_FRACTION:
            return None
        return max(0.0, self.total_joules - baseline_joules)

    def _baseline_joules(self) -> float | None:
        if self.idle_watts <= 0:
            return None
        if self.gpu_idle_watts > BUSY_GPU_WATTS:
            return None
        return self.idle_watts * self.seconds

    @property
    def gpu_sources(self) -> str:
        nvml, firmware = self.gpu_nvml_joules, self.gpu_firmware_joules
        if nvml is None or firmware is None or nvml <= 0 or firmware <= 0:
            return SOURCES_UNMEASURED
        if self.seconds < MIN_COUNTER_WINDOW_SECONDS:
            return SOURCES_UNMEASURED
        ratio = firmware / nvml
        agree = abs(ratio / SOURCE_RATIO - 1.0) <= RATIO_TOLERANCE
        return SOURCES_AGREE if agree else SOURCES_DISAGREE


class Sampler:
    """Reads the box's energy sensors, or reports zeros where there are none."""

    def __init__(
        self,
        nvml: Callable[[], float] | None = None,
        hwmon: Path | None = None,
    ) -> None:
        self.nvml = nvml
        self.hwmon = hwmon
        self.total_power = channel(hwmon, "power", TOTAL_POWER_LABELS)
        self.total_energy = channel(hwmon, "energy", TOTAL_ENERGY_LABELS)
        self.gpu_energy = channel(hwmon, "energy", GPU_ENERGY_LABELS)

    @classmethod
    def detect(cls, root: Path = HWMON) -> Self:
        try:
            chips = sorted(root.glob("hwmon*"))
        except OSError:
            chips = []
        hwmon = None
        for chip in chips:
            try:
                name = (chip / "name").read_text().strip()
            except OSError:
                continue
            if name == CHIP:
                hwmon = chip
                break
        return cls(nvml=nvml_counter(), hwmon=hwmon)

    @property
    def has_energy_counter(self) -> bool:
        return self.total_energy is not None

    def total_watts(self) -> float | None:
        return read_micro(self.total_power)

    def total_joules(self) -> float | None:
        return read_micro(self.total_energy)

    def gpu_firmware_joules(self) -> float | None:
        return read_micro(self.gpu_energy)

    def gpu_nvml_joules(self) -> float | None:
        if self.nvml is None:
            return None
        try:
            value = self.nvml() / 1000.0
        except Exception:  # noqa: BLE001 -- NVML's own error type, and a driver
            return None  # reload invalidates the handle mid-run.
        return value if math.isfinite(value) else None

    def baseline(self, seconds: float) -> Baseline:
        """Idle power over `seconds`, whole-box and GPU-rail, before the run.

        Never call it during the run it is the baseline for."""
        if seconds <= 0:
            return Baseline(0.0, 0.0)
        if self.has_energy_counter:
            box0, gpu0 = self.total_joules(), self.gpu_firmware_joules()
            time.sleep(seconds)
            box1, gpu1 = self.total_joules(), self.gpu_firmware_joules()
            box_delta = delta(box0, box1)
            gpu_delta = delta(gpu0, gpu1)
            return Baseline(
                0.0 if box_delta is None else box_delta / seconds,
                0.0 if gpu_delta is None else gpu_delta / seconds,
            )
        return Baseline(self._gauge_watts(seconds), 0.0)

    def _gauge_watts(self, seconds: float) -> float:
        if self.total_power is None:
            return 0.0
        samples: list[float] = []
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            value = read_micro(self.total_power)
            if value is not None:
                samples.append(value)
            time.sleep(min(SAMPLE_INTERVAL, remaining))
        # Median, not mean: one bad sample past read_micro would swamp a mean.
        return median(samples) if samples else 0.0


def channel(chip: Path | None, kind: str, labels: Sequence[str]) -> Path | None:
    """The `<kind>N_input` file behind the first of `labels` this chip offers."""
    if chip is None:
        return None
    inputs: dict[str, Path] = {}
    for label_file in sorted(chip.glob(f"{kind}*_label")):
        reading = label_file.with_name(
            label_file.name.removesuffix("_label") + "_input"
        )
        try:
            label = label_file.read_text().strip()
        except OSError:
            label = None
        if label is not None and reading.exists():
            inputs.setdefault(label, reading)
    return next((inputs[label] for label in labels if label in inputs), None)


def delta(start: float | None, end: float | None) -> float | None:
    """How far a counter advanced, or None if either endpoint was not read."""
    if start is None or end is None or end < start:
        return None
    return end - start


def read_micro(path: Path | None) -> float | None:
    """A sysfs sensor in base units, or None if it is missing, unparseable,
    non-finite, or the dead-sensor sentinel."""
    if path is None:
        return None
    try:
        micro = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    # Sentinel before the divide, so it means the same for power and for energy.
    if not math.isfinite(micro) or micro == U32_SENTINEL:
        return None
    return micro / MICRO


def nvml_counter() -> Callable[[], float] | None:
    """NVML's total energy counter in millijoules, or None where NVML is not
    available. Imported by name: nvidia-ml-py is absent off the box."""
    try:
        nvml = importlib.import_module("pynvml")
    except ImportError:
        return None
    try:
        nvml.nvmlInit()
        device = nvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:  # noqa: BLE001 -- NVML's own error type, raised past the import
        return None
    return functools.partial(nvml.nvmlDeviceGetTotalEnergyConsumption, device)
