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

Everything here reads sysfs directly and every accessor degrades to None rather
than raising. This package is developed on macOS, where none of these paths
exist, and `spbm_enabled` is false on boxes that did not opt in. A missing
sensor is a degraded reading; it is never a reason for a training run to fail.

None rather than 0.0, because the two are different claims: 0 J says the run
drew nothing, which is false, and it is false in the direction that flatters
whatever configuration happened to run on the sensorless box. Unknown energy is
carried as None all the way to the index, which omits the sample, so a run that
was not measured has no row rather than a row of zeroes.
"""

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

RATIO_TOLERANCE = 0.15
"""RELATIVE, not absolute. The ratio moves only 2.1% across every regime ever
observed (1.198, 1.225, 1.223, 1.195), so an absolute +/-0.5 was 41% of the
ratio and caught a firmware reset only after it had eaten 41% of the run. 15% is
still 7x the observed spread."""

BUSY_GPU_WATTS = 10.0
"""Above this, the GPU rail was already working before the run started, so the
baseline is somebody else's job and marginal energy cannot be attributed to this
run. The rail idles at 3.82 W on this box and draws 73.7 W saturating; 10 W is
2.6x idle. UNVERIFIED on any other box: it is calibrated to THIS hardware, which
is why every summary.json now records idle_gpu_watts so the next revision of this
number comes from records rather than a second guess."""

UNDER_BASELINE_FRACTION = 0.9
"""A run whose total came in more than ~10% under its own baseline means the
neighbour stopped mid-run, so the baseline describes a box that no longer exists
and the subtraction is meaningless. marginal is then unknown, not zero."""

MIN_COUNTER_WINDOW_SECONDS = 2.0
"""Below this the two GPU counters cannot be cross-checked: counter update
granularity, not millijoule quantisation, is what breaks a short window, because
it may catch one tick from one source and two from the other. UNVERIFIED: the
real floor is set by the slowest counter's tick and must be measured once by
reading energy4_input at 10 Hz for 20 s and timing the steps."""

U32_SENTINEL = 4294967295.0
"""What a dead sensor reports, in raw micro units, being u32 max. Matched
exactly rather than through a watts ceiling: the previous guard dropped anything
at or above 4000 W, which is under what a large multi-GPU node genuinely draws,
so it discarded real readings as implausible. Checked before the divide, so it
means the same thing for a power channel and an energy accumulator, and no
legitimate value is ever refused for merely being large."""

SOURCES_AGREE = "agree"
SOURCES_DISAGREE = "disagree"
SOURCES_UNMEASURED = "unmeasured"
"""Three states, because the old boolean was wrong in both directions: it read
`disagree` on every run of a box whose driver lacks the NVML counter (a false
alarm), and `agree` on a sensorless box where nothing was measured at all."""


def watt_hours(joules: float) -> float:
    return joules / 3600.0


@dataclass(frozen=True)
class Baseline:
    """Idle power sampled before the run, whole-box and GPU-rail separately.

    The GPU rail is what tells a quiet box from a contended one: idle it is a
    few watts, and a neighbour's job puts it in the tens."""

    idle_watts: float
    gpu_watts: float


@dataclass(frozen=True)
class EnergyReading:
    total_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    gpu_idle_watts: float
    seconds: float
    """The three counters are None when the box has no such sensor, or when
    either end of their delta could not be read. A run whose energy was not
    measured records no energy, rather than recording that it drew nothing."""

    @property
    def marginal_joules(self) -> float | None:
        """Energy attributable to the run, or None when the baseline cannot
        carry the subtraction.

        None, never 0.0: 0.0 conflates "drew nothing above idle" with "the
        baseline belongs to the colleague's job", and the second is common
        enough that clamping to zero told users a contended run cost nothing.
        "Unknown" in Prometheus is an omitted sample, which render() already
        drops, so a contaminated run simply has no marginal row rather than a
        misleading zero."""
        if self.total_joules is None:
            return None  # the run's own energy was never measured
        baseline_joules = self._baseline_joules()
        if baseline_joules is None:
            return None
        if self.total_joules < baseline_joules * UNDER_BASELINE_FRACTION:
            return None  # the neighbour stopped mid-run; the baseline is stale
        # Within 10% under is measurement noise, not free energy: clamp to zero.
        return max(0.0, self.total_joules - baseline_joules)

    def _baseline_joules(self) -> float | None:
        """What idle would have cost over the window, or None when there is
        no baseline the subtraction can trust."""
        if self.idle_watts <= 0:
            return None  # no baseline was measured
        if self.gpu_idle_watts > BUSY_GPU_WATTS:
            return None  # the GPU rail was already busy: someone else's job
        return self.idle_watts * self.seconds

    @property
    def gpu_sources(self) -> str:
        """Which of the three states the two GPU counters are in.

        `disagree` means one reset mid-run, which D2's backwards-delta guard
        cannot catch because a reload that re-accumulates past the start value
        gives a wrong but positive delta. A window too short to cross-check, or
        a run only one source measured, is `unmeasured` rather than a false
        `disagree`."""
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

    @property
    def has_energy_counter(self) -> bool:
        """Whether a whole-box energy accumulator exists to read a baseline from
        as a counter delta, rather than integrating the power gauge."""
        return self.total_energy is not None

    def total_watts(self) -> float | None:
        """Instantaneous whole-box draw, or None where it cannot be read."""
        return _read_micro(self.total_power)

    def total_joules(self) -> float | None:
        """Whole-box energy counter. Read as a delta across the run."""
        return _read_micro(self.total_energy)

    def gpu_firmware_joules(self) -> float | None:
        """The GPU rail's energy counter. Read as a delta across the run."""
        return _read_micro(self.gpu_energy)

    def gpu_nvml_joules(self) -> float | None:
        """The GPU domain's energy counter, which NVML reports in millijoules.

        It resets on driver reload, which is why `EnergyReading` cross-checks it
        against the firmware counter rather than trusting a positive delta."""
        if self.nvml is None:
            return None
        try:
            value = self.nvml() / 1000.0
        except Exception:  # NVML's own error type, and a reload invalidates the
            return None  # handle mid-run; either way the reading is degraded.
        return value if math.isfinite(value) else None

    def baseline(self, seconds: float) -> Baseline:
        """Idle power over `seconds`, whole-box and GPU-rail, before the run.

        Read from the SAME counters the run is measured against, as a delta
        across the window: a counter delta divided by its window IS the average
        power, exactly, with none of the gauge's short-window integral error.
        The gauge loop remains the fallback for a box that exposes power but no
        accumulator.

        Call it before anything else expensive and never during the run it is
        the baseline for: even the fallback loop costs ~0.8 W while it runs.
        """
        if seconds <= 0:
            return Baseline(0.0, 0.0)
        if self.has_energy_counter:
            box0, gpu0 = self.total_joules(), self.gpu_firmware_joules()
            time.sleep(seconds)
            box1, gpu1 = self.total_joules(), self.gpu_firmware_joules()
            # An endpoint that could not be read leaves the rail at 0 W, which
            # is how "no baseline" is spelled to marginal_joules: it then
            # declines to subtract rather than subtracting a fabricated number.
            return Baseline(
                _watts(delta(box0, box1), seconds),
                _watts(delta(gpu0, gpu1), seconds),
            )
        # No accumulator: integrate the power gauge. The GPU rail has no gauge
        # here, so its idle draw is unknown and reported as zero.
        return Baseline(self._gauge_watts(seconds), 0.0)

    def _gauge_watts(self, seconds: float) -> float:
        """Median whole-box draw over `seconds`, sampled at 1 Hz.

        Median, not mean: a single u32-sentinel sample (4295 W) drags the mean
        of 59 real 13 W samples to 84 W but leaves the median at 13 W. Bad
        samples are dropped outright by `_read_micro` returning None, and the
        median guards against any that slip a plausible-looking value through.
        """
        if self.total_power is None:
            return 0.0
        samples: list[float] = []
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            value = _read_micro(self.total_power)
            if value is not None:
                samples.append(value)
            time.sleep(min(SAMPLE_INTERVAL, remaining))
        return median(samples) if samples else 0.0


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


def _watts(joules: float | None, seconds: float) -> float:
    """A counter delta as average power, or 0 W when the delta is unknown.

    The Baseline stays a plain float because 0 W already means "no baseline
    worth subtracting" everywhere downstream; introducing a second spelling of
    absent would give marginal_joules two None branches that behave alike."""
    return 0.0 if joules is None else joules / seconds


def delta(start: float | None, end: float | None) -> float | None:
    """How far a counter advanced, or None if either endpoint was not read.

    Coalescing a failed read to 0.0 is the trap this exists to avoid: a glitched
    START read makes the delta the entire accumulator, which on this box is
    ~5 GJ reported as one run's energy. Neither endpoint is optional, because
    the difference is only meaningful when both were measured. A backwards
    delta is a counter reset mid-window, which is also not a measurement.
    """
    if start is None or end is None or end < start:
        return None
    return end - start


def _read_micro(path: Path | None) -> float | None:
    """A sysfs sensor in base units, or None if it is missing, unparseable,
    non-finite, or the dead-sensor sentinel.

    None rather than 0.0 so a caller sampling a stream can drop the bad reading
    instead of averaging a zero into it. `float("nan")` and `float("inf")` parse
    without raising, which is how a bare NaN token would otherwise reach
    json.dumps and produce a file every strict parser rejects.
    """
    raw = _read(path) if path is not None else None
    if raw is None:
        return None
    try:
        micro = float(raw)
    except ValueError:
        return None
    if not math.isfinite(micro) or micro == U32_SENTINEL:
        return None
    return micro / MICRO


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
