"""Host telemetry during a latency run, to tell a slow engine from a busy machine.

Samples once a second (Linux; elsewhere it records nothing): host CPU, the engine container's
CPU and quota throttling, the benchmark's own CPU (its cgroup, which holds the client
processes), what is left over ("other": anything else on the host), pressure stall
information, CPU frequency and temperature, swapping. Raw samples go to
results/telemetry/<run>.jsonl; summary() returns columns for the latency row.
"""
import glob
import json
import os
import subprocess
import threading
import time
from functools import cache
from pathlib import Path

from . import config

# Warning thresholds: other load in cores, CPU pressure in % of time.
OTHER_CORES_WARN = 1.0
CPU_PSI_WARN = 5.0


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def service_of(engine: str) -> str:
    """The compose service an engine arm runs in."""
    if engine.startswith("elasticsearch"):
        return "elasticsearch"
    return "pg-textsearch" if engine.startswith("pg-textsearch") else "postgres"


@cache
def container_cgroup(service: str) -> str | None:
    try:
        cid = subprocess.run(["docker", "compose", "ps", "-q", "--no-trunc", service],
                             cwd=config.ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    path = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
    return path if cid and os.path.isdir(path) else None


def own_cgroup() -> str | None:
    text = _read("/proc/self/cgroup")
    if not text:
        return None
    path = "/sys/fs/cgroup" + text.strip().split("::", 1)[-1]
    return path if os.path.isdir(path) else None


def _cpu_stat(cgroup: str | None) -> dict:
    text = _read(f"{cgroup}/cpu.stat") if cgroup else None
    return {k: int(v) for k, v in (l.split() for l in text.splitlines())} if text else {}


def _psi(kind: str) -> int | None:
    text = _read(f"/proc/pressure/{kind}")
    if not text:
        return None
    some = text.splitlines()[0]  # "some avg10=.. avg60=.. avg300=.. total=<usec>"
    return int(some.rsplit("total=", 1)[1])


def _snapshot(container: str | None, own: str | None) -> dict:
    s: dict = {"t": time.monotonic()}
    stat = _read("/proc/stat")
    if stat:
        f = [int(x) for x in stat.splitlines()[0].split()[1:9]]
        s["cpu_jiffies"] = {"busy": f[0] + f[1] + f[2] + f[5] + f[6] + f[7],
                            "iowait": f[4], "steal": f[7], "total": sum(f)}
    c = _cpu_stat(container)
    if c:
        s["container"] = {k: c.get(k, 0) for k in ("usage_usec", "nr_throttled", "throttled_usec")}
    o = _cpu_stat(own)
    if o:
        s["own_usec"] = o.get("usage_usec", 0)
    s["psi"] = {k: _psi(k) for k in ("cpu", "io", "memory")}
    freqs = [int(_read(p) or 0) for p in
             glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    if freqs:
        s["freq_mhz"] = [min(freqs) // 1000, sum(freqs) // len(freqs) // 1000]
    temps = [int(_read(p) or 0) for p in glob.glob("/sys/class/thermal/thermal_zone*/temp")]
    if temps:
        s["temp_c"] = max(temps) / 1000
    vm = _read("/proc/vmstat")
    if vm:
        d = dict(l.split() for l in vm.splitlines())
        s["swap_pages"] = int(d.get("pswpin", 0)) + int(d.get("pswpout", 0))
    mem = _read("/proc/meminfo")
    if mem:
        d = {l.split(":")[0]: l.split()[1] for l in mem.splitlines()}
        s["mem_available_mb"] = int(d["MemAvailable"]) // 1024
    return s


class Sampler:
    def __init__(self, service: str | None, run_name: str, interval: float = 1.0):
        self.container = container_cgroup(service) if service else None
        self.own = own_cgroup()
        self.interval, self.run_name = interval, run_name
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.enabled = os.path.exists("/proc/stat")

    def __enter__(self):
        if self.enabled:
            self.samples.append(_snapshot(self.container, self.own))
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self.enabled:
            self._stop.set()
            self._thread.join()
            self.samples.append(_snapshot(self.container, self.own))
            out = config.RESULTS / "telemetry" / f"{self.run_name}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(json.dumps(s) + "\n" for s in self.samples))

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.samples.append(_snapshot(self.container, self.own))

    def summary(self) -> dict:
        if len(self.samples) < 2:
            return {}
        cores = os.cpu_count() or 1
        host, cont, own, other = [], [], [], []
        for a, b in zip(self.samples, self.samples[1:]):
            dt = b["t"] - a["t"]
            if dt <= 0 or "cpu_jiffies" not in a:
                continue
            ja, jb = a["cpu_jiffies"], b["cpu_jiffies"]
            busy_cores = (jb["busy"] - ja["busy"]) / max(1, jb["total"] - ja["total"]) * cores
            c = (b["container"]["usage_usec"] - a["container"]["usage_usec"]) / 1e6 / dt \
                if "container" in a else 0.0
            o = (b["own_usec"] - a["own_usec"]) / 1e6 / dt if "own_usec" in a else 0.0
            host.append(busy_cores); cont.append(c); own.append(o)
            other.append(max(0.0, busy_cores - c - o))
        first, last = self.samples[0], self.samples[-1]
        elapsed = last["t"] - first["t"]
        row = {
            "tel_host_cores_mean": round(sum(host) / len(host), 2) if host else "",
            "tel_container_cores_mean": round(sum(cont) / len(cont), 2) if cont else "",
            "tel_client_cores_mean": round(sum(own) / len(own), 2) if own else "",
            "tel_other_cores_mean": round(sum(other) / len(other), 2) if other else "",
            "tel_other_cores_max": round(max(other), 2) if other else "",
        }
        if "container" in first:
            row["tel_throttled_ms"] = round((last["container"]["throttled_usec"]
                                             - first["container"]["throttled_usec"]) / 1000)
        for kind in ("cpu", "io", "memory"):
            a, b = first["psi"].get(kind), last["psi"].get(kind)
            if a is not None and b is not None and elapsed > 0:
                row[f"tel_{kind}_psi_pct"] = round((b - a) / 1e6 / elapsed * 100, 2)
        if "cpu_jiffies" in first:
            ja, jb = first["cpu_jiffies"], last["cpu_jiffies"]
            row["tel_steal_pct"] = round((jb["steal"] - ja["steal"]) / max(1, jb["total"] - ja["total"]) * 100, 2)
        freqs = [s["freq_mhz"] for s in self.samples if "freq_mhz" in s]
        if freqs:  # idle cores clock down, so the mean says more than the lowest core
            row["tel_freq_mhz_mean"] = sum(f[1] for f in freqs) // len(freqs)
            row["tel_freq_mhz_min"] = min(f[0] for f in freqs)
        temps = [s["temp_c"] for s in self.samples if "temp_c" in s]
        if temps:
            row["tel_temp_c_max"] = max(temps)
        if "swap_pages" in first:
            row["tel_swap_pages"] = last["swap_pages"] - first["swap_pages"]
        mems = [s["mem_available_mb"] for s in self.samples if "mem_available_mb" in s]
        if mems:
            row["tel_mem_available_mb_min"] = min(mems)
        warn = []
        if other and max(other) > OTHER_CORES_WARN:
            warn.append(f"other load up to {max(other):.1f} cores")
        if row.get("tel_throttled_ms", 0) > 0:
            warn.append(f"container throttled {row['tel_throttled_ms']} ms")
        if row.get("tel_cpu_psi_pct", 0) > CPU_PSI_WARN:
            warn.append(f"cpu pressure {row['tel_cpu_psi_pct']}%")
        if row.get("tel_swap_pages", 0) > 0:
            warn.append("swapping")
        row["tel_warning"] = "; ".join(warn)
        return row
