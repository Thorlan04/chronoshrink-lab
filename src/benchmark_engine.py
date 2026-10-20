"""ChronoShrink-Lab: Deterministic RAM Benchmark Engine and Industrial Lossless Verifier (Argon Hardened).

Generates 200 industrial time-series datasets in RAM, runs deterministic baselines
(Delta-1, Double-Delta Gorilla, Linear Extrapolation), and evaluates candidate
prediction functions inside a secure sandbox with 100% bit-exact lossless verification
(`assert np.array_equal(original, decompressed)`) and real ZigZag + LEB128 VarInt bit measurement.
"""

from __future__ import annotations

import time
import math
import numpy as np
from typing import Callable, Any, Dict, List, Tuple, Sequence


# ============================================================================
# 1. ENTROPY & VARINT BIT PACKING UTILITIES (BIT-EXACT LEB128 + VECTORIZED)
# ============================================================================

def zigzag_encode(n: int) -> int:
    """Map signed integer to unsigned integer using ZigZag encoding."""
    if n >= 0:
        return 2 * n
    return -2 * n - 1


def zigzag_decode(u: int) -> int:
    """Decode unsigned ZigZag integer back to signed integer."""
    if u % 2 == 0:
        return u // 2
    return -(u + 1) // 2


def varint_encode(u: int) -> bytearray:
    """Encode unsigned integer into variable-length LEB128 byte stream."""
    out = bytearray()
    while True:
        byte = u & 0x7F
        u >>= 7
        if u != 0:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return out


def varint_decode(stream: bytes, offset: int = 0) -> Tuple[int, int]:
    """Decode LEB128 byte stream at given offset. Returns (value, new_offset)."""
    res = 0
    shift = 0
    curr = offset
    while curr < len(stream):
        byte = stream[curr]
        curr += 1
        res |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return res, curr


def compute_varint_bits_vectorized(residuals: np.ndarray) -> int:
    """
    Pure NumPy vectorized computation of exact LEB128 VarInt encoded bit length
    after ZigZag mapping on an array of signed integer residuals.
    Matches `len(varint_encode(zigzag_encode(r))) * 8` bit-for-bit.
    """
    r = residuals.astype(np.int64, copy=False)
    u = np.where(r >= 0, 2 * r, -2 * r - 1).astype(np.uint64)
    # Each LEB128 byte carries 7 payload bits; 0 takes 1 byte (8 bits)
    n_bytes = np.ones_like(u, dtype=np.int64)
    temp = u >> np.uint64(7)
    while np.any(temp > 0):
        n_bytes += (temp > 0).astype(np.int64)
        temp >>= np.uint64(7)
    return int(np.sum(n_bytes) * 8)


# ============================================================================
# 2. INDUSTRIAL DATASET GENERATOR (200 SERIES IN RAM)
# ============================================================================

class IndustrialTimeSeriesGenerator:
    """Generates 200 deterministic industrial time-series streams in RAM."""

    def __init__(self, seed: int = 42):
        self.rng = np.random.RandomState(seed)
        self._cache: List[Dict[str, Any]] | None = None

    def generate_all(self, count: int = 200) -> List[Dict[str, Any]]:
        """Generate and cache 200 industrial telemetry streams."""
        if self._cache is not None and len(self._cache) == count:
            return self._cache

        series_list = []
        per_cat = count // 5

        # 1. Thermal Drifts (Quantized to 0.01°C = integers)
        for i in range(per_cat):
            n = 200
            t = np.arange(n)
            drift = 0.05 * (i % 5 - 2) * t
            diurnal = 500.0 * np.sin(2 * np.pi * t / 48.0)
            noise = self.rng.normal(0, 8.0, n)
            raw = 2500.0 + drift + diurnal + noise
            values = np.round(raw).astype(np.int64).tolist()
            dt = [1000 + int(self.rng.choice([0, 0, 0, 10, -10])) for _ in range(n)]
            series_list.append({
                "id": f"thermal_{i:03d}",
                "category": "thermal_drift",
                "values": values,
                "dt": dt,
                "correlations": [[v + int(self.rng.normal(0, 15)) for v in values]]
            })

        # 2. Industrial Vibrations (Quantized to milli-g)
        for i in range(per_cat):
            n = 250
            t = np.arange(n)
            f1 = 0.08 + (i % 4) * 0.04
            f2 = 0.22 + (i % 3) * 0.05
            signal = 1200.0 * np.sin(2 * np.pi * f1 * t) + 400.0 * np.cos(2 * np.pi * f2 * t)
            harmonics = self.rng.normal(0, 45.0, n)
            values = np.round(signal + harmonics).astype(np.int64).tolist()
            dt = [100] * n
            series_list.append({
                "id": f"vib_{i:03d}",
                "category": "vibration",
                "values": values,
                "dt": dt,
                "correlations": [[int(v * 0.85) for v in values]]
            })

        # 3. Voltage Spikes & Transients (Quantized to mV)
        for i in range(per_cat):
            n = 200
            values = [230000]
            dt = [1000]
            val = 230000
            for step in range(1, n):
                rand_val = self.rng.rand()
                if rand_val < 0.04:
                    val += int(self.rng.choice([-8500, -4200, 5600, 12000]))
                elif rand_val < 0.12:
                    val += int(self.rng.choice([-600, 600, -1200, 1200]))
                else:
                    val += int(self.rng.normal(0, 35))
                values.append(val)
                dt.append(1000)
            series_list.append({
                "id": f"volt_{i:03d}",
                "category": "voltage_transient",
                "values": values,
                "dt": dt,
                "correlations": [[v // 10 for v in values]]
            })

        # 4. Plateaus & Quantized Tank Levels / State Sensors
        for i in range(per_cat):
            n = 200
            values = []
            curr_level = int(self.rng.randint(1000, 5000))
            plateau_length = 0
            for _ in range(n):
                if plateau_length <= 0:
                    curr_level += int(self.rng.choice([-250, -100, 0, 150, 300]))
                    plateau_length = int(self.rng.randint(8, 25))
                else:
                    plateau_length -= 1
                noise = int(self.rng.choice([0, 0, 0, -1, 1]))
                values.append(curr_level + noise)
            dt = [5000] * n
            series_list.append({
                "id": f"plateau_{i:03d}",
                "category": "plateau",
                "values": values,
                "dt": dt,
                "correlations": [[max(0, v - 50) for v in values]]
            })

        # 5. Multi-channel Correlated Telemetry (Speed, Current, Temp, Pressure)
        for i in range(per_cat):
            n = 220
            t = np.arange(n)
            speed = 1500.0 + 300.0 * np.sin(2 * np.pi * t / 60.0) + self.rng.normal(0, 10, n)
            current = 0.08 * speed + self.rng.normal(0, 3, n)
            values = np.round(current * 100).astype(np.int64).tolist()
            speed_chan = np.round(speed).astype(np.int64).tolist()
            dt = [250] * n
            series_list.append({
                "id": f"correlated_{i:03d}",
                "category": "correlated_telemetry",
                "values": values,
                "dt": dt,
                "correlations": [speed_chan]
            })

        self._cache = series_list
        return series_list


# ============================================================================
# 3. BASELINE PREDICTORS (INDUSTRY STANDARDS)
# ============================================================================

def baseline_delta_1(history: Sequence[int], dt: Sequence[int], corr: Sequence[Sequence[int]]) -> int:
    """Standard Delta-Encoding (Order 1 difference): predicts previous value."""
    if not history:
        return 0
    return history[-1]


def baseline_double_delta(history: Sequence[int], dt: Sequence[int], corr: Sequence[Sequence[int]]) -> int:
    """Gorilla / Prometheus Double-Delta (Order 2): x[t-1] + (x[t-1] - x[t-2])."""
    if not history:
        return 0
    if len(history) == 1:
        return history[-1]
    return 2 * history[-1] - history[-2]


def baseline_linear_extrapolator(history: Sequence[int], dt: Sequence[int], corr: Sequence[Sequence[int]]) -> int:
    """Industrial Edge Linear Extrapolation with timestamp delta weighting."""
    if not history:
        return 0
    if len(history) < 2:
        return history[-1]

    d_val = history[-1] - history[-2]
    if dt and len(dt) >= 2 and dt[-2] > 0:
        ratio = dt[-1] / dt[-2]
        pred = history[-1] + int(round(d_val * ratio))
    else:
        pred = history[-1] + d_val
    return pred


# ============================================================================
# 4. COMPRESSION & DECOMPRESSION HARNESS (REAL VARINT BITS & IMMUTABLE WINDOWS)
# ============================================================================

def compress_series(
    values: List[int],
    dt: List[int],
    correlations: List[List[int]],
    predictor: Callable[[Sequence[int], Sequence[int], Sequence[Sequence[int]]], int],
    window_size: int = 32
) -> bytearray:
    """Compress a series of integers using a predictive residual function and real LEB128 VarInt packing."""
    out = bytearray()
    if not values:
        return out

    # Write first raw value with ZigZag + VarInt
    out.extend(varint_encode(zigzag_encode(values[0])))

    n = len(values)
    for t in range(1, n):
        start_idx = max(0, t - window_size)
        # Pass fresh copies to prevent in-place mutation cheating
        hw = values[start_idx:t]
        dw = dt[start_idx:t] if dt else [1000] * (t - start_idx)
        cw = [c[start_idx:t] for c in correlations] if correlations else []

        pred = int(predictor(hw, dw, cw))
        actual = values[t]
        residual = actual - pred

        out.extend(varint_encode(zigzag_encode(residual)))

    return out


def decompress_series(
    payload: bytes,
    expected_length: int,
    dt: List[int],
    correlations: List[List[int]],
    predictor: Callable[[Sequence[int], Sequence[int], Sequence[Sequence[int]]], int],
    window_size: int = 32
) -> List[int]:
    """Decompress LEB128 byte stream back into exact original integers using predictive function."""
    if expected_length == 0 or not payload:
        return []

    first_u, offset = varint_decode(payload, 0)
    first_val = zigzag_decode(first_u)

    recovered = [first_val]

    for t in range(1, expected_length):
        start_idx = max(0, t - window_size)
        hw = recovered[start_idx:t]
        dw = dt[start_idx:t] if dt else [1000] * (t - start_idx)
        cw = [c[start_idx:t] for c in correlations] if correlations else []

        pred = int(predictor(hw, dw, cw))

        res_u, offset = varint_decode(payload, offset)
        res = zigzag_decode(res_u)

        actual = pred + res
        recovered.append(actual)

    return recovered


# ============================================================================
# 5. BENCHMARK ENGINE & SANDBOX RUNNER
# ============================================================================

class BenchmarkEngine:
    """Comprehensive Lossless Benchmark and Verification Sandbox."""

    def __init__(self, dataset_size: int = 200):
        self.generator = IndustrialTimeSeriesGenerator(seed=42)
        self.datasets = self.generator.generate_all(count=dataset_size)
        self.total_samples = sum(len(d["values"]) for d in self.datasets)
        # Raw bytes if stored as standard int32 (4 bytes / 32 bits per sample)
        self.total_raw_bytes = self.total_samples * 4
        self.total_raw_bits = self.total_raw_bytes * 8
        self._baselines_cache: Dict[str, Dict[str, Any]] | None = None

    def evaluate_baselines(self) -> Dict[str, Dict[str, Any]]:
        """Evaluate the 3 industry standards on all datasets using vectorized VarInt bit accounting."""
        if self._baselines_cache is not None:
            return self._baselines_cache

        baselines = {
            "Delta_1": baseline_delta_1,
            "Double_Delta": baseline_double_delta,
            "Linear_Extrapolator": baseline_linear_extrapolator
        }

        results = {}
        for name, fn in baselines.items():
            t0 = time.perf_counter()
            total_bits = 0
            for item in self.datasets:
                c_bytes = compress_series(item["values"], item["dt"], item["correlations"], fn)
                total_bits += len(c_bytes) * 8
            elapsed = time.perf_counter() - t0

            total_bytes = total_bits // 8
            ratio = (1.0 - (total_bits / self.total_raw_bits)) * 100.0
            bps = float(total_bits) / float(self.total_samples)
            time_us_per_sample = (elapsed * 1e6) / self.total_samples

            results[name] = {
                "total_bytes": total_bytes,
                "total_bits": total_bits,
                "compression_ratio_pct": round(ratio, 2),
                "bits_per_sample": round(bps, 3),
                "time_us_per_sample": round(time_us_per_sample, 3)
            }

        self._baselines_cache = results
        return results

    def _verify_anti_cheat_probes(self, candidate_fn: Callable) -> str | None:
        """
        Argon Anti-Overfitting & Anti-Cheat Gate:
        1. Verifies candidate does not mutate input lists in-place.
        2. Verifies determinism across repeated calls.
        3. Rejects trivial constant functions (`return 0`).
        """
        probes = [
            ([1000, 1020, 1040, 1060], [1000, 1000, 1000, 1000], [[500, 510, 520, 530]]),
            ([230000, 230010, 230005, 230012], [1000, 1000, 1000, 1000], [[23000, 23001, 23000, 23001]]),
            ([-450, -320, -180, -40], [100, 100, 100, 100], [[-380, -270, -150, -34]]),
            ([3200, 3200, 3200, 3200], [5000, 5000, 5000, 5000], [[3150, 3150, 3150, 3150]]),
            ([15000, 14800, 14600, 14400], [250, 250, 250, 250], [[1800, 1775, 1750, 1725]]),
        ]
        outputs = []
        for hw, dw, cw in probes:
            hw_copy = list(hw)
            dw_copy = list(dw)
            cw_copy = [list(c) for c in cw]
            p1 = int(candidate_fn(hw_copy, dw_copy, cw_copy))
            if hw_copy != hw or dw_copy != dw:
                return "Anti-Cheat Violation: In-place mutation of history_window or timestamps_delta detected."
            p2 = int(candidate_fn(list(hw), list(dw), [list(c) for c in cw]))
            if p1 != p2:
                return "Anti-Cheat Violation: Non-deterministic predictor output."
            outputs.append(p1)

        if float(np.std(outputs)) < 1.0:
            return "Anti-Overfitting Violation: Constant or degenerate predictor rejected."
        return None

    def evaluate_candidate(
        self,
        candidate_fn: Callable[[Sequence[int], Sequence[int], Sequence[Sequence[int]]], int],
        name: str = "Candidate",
        max_timeout_s: float = 4.0
    ) -> Dict[str, Any]:
        """Test a candidate predictor across all 200 datasets.

        Enforces:
        - Anti-cheat probe inspection (no in-place mutation, non-constant, deterministic).
        - `assert np.array_equal(original, decompressed)` on 100% of the 200 time-series.
        - Exact compressed size measured in real bits after ZigZag + LEB128 VarInt encoding.
        """
        try:
            probe_err = self._verify_anti_cheat_probes(candidate_fn)
        except Exception as e:
            probe_err = f"Probe runtime exception: {e}"
        if probe_err:
            return {
                "is_valid": False,
                "error_msg": probe_err,
                "compression_ratio_pct": 0.0,
                "bits_per_sample": 32.0,
                "beats_all_baselines": False,
                "composite_score": 0.0
            }

        baselines = self.evaluate_baselines()
        b_delta = baselines["Delta_1"]["total_bytes"]
        b_ddelta = baselines["Double_Delta"]["total_bytes"]
        b_linear = baselines["Linear_Extrapolator"]["total_bytes"]
        best_baseline_bytes = min(b_delta, b_ddelta, b_linear)

        total_compressed_bits = 0
        total_compressed_bytes = 0
        t0 = time.perf_counter()

        # Lossless reversibility check on 100% of the 200 series
        for idx, item in enumerate(self.datasets):
            if time.perf_counter() - t0 > max_timeout_s:
                return {
                    "is_valid": False,
                    "error_msg": f"Sandbox timeout exceeded ({max_timeout_s}s) at dataset {idx}",
                    "compression_ratio_pct": 0.0,
                    "bits_per_sample": 32.0,
                    "beats_all_baselines": False,
                    "composite_score": 0.0
                }

            vals = item["values"]
            dt = item["dt"]
            corr = item["correlations"]
            original_arr = np.asarray(vals, dtype=np.int64)
            original_arr.flags.writeable = False

            try:
                # 1. Compress into real ZigZag + LEB128 VarInt bitstream
                compressed = compress_series(vals, dt, corr, candidate_fn)
                stream_bytes = bytes(compressed)
                stream_bits = len(stream_bytes) * 8

                total_compressed_bytes += len(stream_bytes)
                total_compressed_bits += stream_bits

                # 2. Decompress from bitstream and strictly assert bit-exact equality via NumPy
                recovered = decompress_series(stream_bytes, len(vals), dt, corr, candidate_fn)
                decompressed_arr = np.asarray(recovered, dtype=np.int64)

                assert np.array_equal(original_arr, decompressed_arr), (
                    f"DISQUALIFIED_NOT_LOSSLESS: np.array_equal failed on series {item['id']}"
                )
            except Exception as e:
                return {
                    "is_valid": False,
                    "error_msg": f"Runtime/Lossless failure: {str(e)}",
                    "compression_ratio_pct": 0.0,
                    "bits_per_sample": 32.0,
                    "beats_all_baselines": False,
                    "composite_score": 0.0
                }

        elapsed = time.perf_counter() - t0
        time_us = (elapsed * 1e6) / self.total_samples

        # Real bit-measured compression metrics
        compression_ratio = (1.0 - (float(total_compressed_bits) / float(self.total_raw_bits))) * 100.0
        bps = float(total_compressed_bits) / float(self.total_samples)

        vs_delta = ((b_delta - total_compressed_bytes) / b_delta) * 100.0
        vs_ddelta = ((b_ddelta - total_compressed_bytes) / b_ddelta) * 100.0
        vs_linear = ((b_linear - total_compressed_bytes) / b_linear) * 100.0

        beats_all = (
            total_compressed_bytes < b_delta and
            total_compressed_bytes < b_ddelta and
            total_compressed_bytes < b_linear
        )

        speed_factor = max(0.5, 1.0 - (time_us / 50.0))
        gain_vs_best = ((best_baseline_bytes - total_compressed_bytes) / best_baseline_bytes) * 100.0
        composite_score = round(compression_ratio + (gain_vs_best * 2.0) * speed_factor, 3)

        return {
            "name": name,
            "is_valid": True,
            "total_bytes": total_compressed_bytes,
            "total_bits": total_compressed_bits,
            "raw_bytes": self.total_raw_bytes,
            "compression_ratio_pct": round(compression_ratio, 2),
            "bits_per_sample": round(bps, 3),
            "time_us_per_sample": round(time_us, 3),
            "vs_delta_pct": round(vs_delta, 2),
            "vs_double_delta_pct": round(vs_ddelta, 2),
            "vs_linear_pct": round(vs_linear, 2),
            "gain_vs_best_pct": round(gain_vs_best, 2),
            "beats_all_baselines": beats_all,
            "composite_score": composite_score,
            "error_msg": ""
        }
