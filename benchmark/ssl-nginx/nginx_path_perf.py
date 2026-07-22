#!/usr/bin/env python3
"""Short ssl-nginx perf accounting for nginx workers and sslsniff readers."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "benchmark" / "ssl-nginx"
NGINX_CONF = BENCH_DIR / "nginx.conf"
INDEX_HTML = BENCH_DIR / "index.html"
SSLSNIFF = ROOT / "example" / "sslsniff" / "sslsniff"
AGENT = ROOT / "build" / "runtime" / "agent" / "libbpftime-agent.so"
SYSCALL_SERVER = (
    ROOT / "build" / "runtime" / "syscall-server" / "libbpftime-syscall-server.so"
)
BPFTIMETOOL = ROOT / "build" / "tools" / "bpftimetool" / "bpftimetool"
URL = "https://127.0.0.1:4043/index.html"
PERF_EVENTS = (
    "task-clock",
    "cycles:u",
    "instructions:u",
    "cycles:k",
    "instructions:k",
    "context-switches",
    "cpu-migrations",
)
MODES = ("baseline", "kernel-global", "kernel-nginx-only", "bpftime")


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run(
    command: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
    )


def require_paths() -> None:
    for path in (NGINX_CONF, SSLSNIFF, AGENT, SYSCALL_SERVER, BPFTIMETOOL):
        if not path.exists():
            raise SystemExit(f"Required path does not exist: {path}")
    for command in ("nginx", "wrk", "curl"):
        if shutil.which(command) is None:
            raise SystemExit(f"Required command does not exist: {command}")


def process_pids(comm: str) -> list[int]:
    completed = run(["pgrep", "-x", comm], check=False, capture_output=True)
    return [int(value) for value in completed.stdout.split() if value.isdigit()]


def cleanup() -> None:
    for comm in ("wrk", "sslsniff", "nginx"):
        run(["sudo", "-n", "pkill", "-TERM", "-x", comm], check=False)
    time.sleep(0.4)
    for comm in ("wrk", "sslsniff", "nginx"):
        run(["sudo", "-n", "pkill", "-KILL", "-x", comm], check=False)
    run(["sudo", "-n", str(BPFTIMETOOL), "remove"], check=False)
    for path in (BENCH_DIR / "nginx.pid", BENCH_DIR / "access.log"):
        path.unlink(missing_ok=True)


def wait_for_worker(master_pid: int, timeout: float = 10.0) -> int:
    children_path = Path(f"/proc/{master_pid}/task/{master_pid}/children")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            children = [int(value) for value in children_path.read_text().split()]
        except (FileNotFoundError, PermissionError):
            children = []
        for pid in children:
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip() == "nginx":
                    return pid
            except FileNotFoundError:
                continue
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for nginx worker under master {master_pid}")


def wait_for_https(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = run(
            ["curl", "-k", "-sS", "-o", "/dev/null", "-w", "%{http_code}", URL],
            check=False,
            capture_output=True,
        )
        if completed.returncode == 0 and completed.stdout == "200":
            return
        time.sleep(0.2)
    raise RuntimeError("Timed out waiting for nginx HTTPS endpoint")


def parse_wrk(output: str) -> float:
    match = re.search(r"Requests/sec:\s+([0-9]+(?:\.[0-9]+)?)", output)
    if not match:
        raise RuntimeError(f"Unable to parse wrk output:\n{output}")
    return float(match.group(1))


def parse_perf(path: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    with path.open(newline="") as handle:
        for row in csv.reader(handle):
            if not row or row[0].startswith("#") or len(row) < 3:
                continue
            raw = row[0].strip().replace(" ", "")
            event = row[2].strip()
            if not raw or raw.startswith("<"):
                continue
            try:
                values[event] = float(raw)
            except ValueError:
                continue
    return values


def launch_perf(perf: str, pid: int, output: Path, seconds: int) -> subprocess.Popen[str]:
    command = [
        "sudo",
        "-n",
        perf,
        "stat",
        "-x,",
        "-o",
        str(output),
        "-e",
        ",".join(PERF_EVENTS),
        "-p",
        str(pid),
        "--timeout",
        str(seconds * 1000),
    ]
    return subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def start_nginx(case_dir: Path, bpftime: bool) -> tuple[subprocess.Popen[bytes], int]:
    environment = os.environ.copy()
    if bpftime:
        environment["LD_PRELOAD"] = str(AGENT)
        environment["BPFTIME_LOG_OUTPUT"] = str(case_dir / "runtime.log")
        environment["SPDLOG_LEVEL"] = "info"
    stderr = (case_dir / "nginx.stderr").open("wb")
    process = subprocess.Popen(
        [
            "nginx",
            "-c",
            str(NGINX_CONF),
            "-p",
            str(BENCH_DIR),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )
    worker_pid = wait_for_worker(process.pid)
    wait_for_https()
    return process, worker_pid


def start_kernel_reader(
    case_dir: Path, nginx_worker_pid: int | None
) -> tuple[subprocess.Popen[bytes], int]:
    command = ["sudo", "-n", str(SSLSNIFF)]
    if nginx_worker_pid is not None:
        command.extend(["-p", str(nginx_worker_pid)])
    stderr = (case_dir / "sslsniff.stderr").open("wb")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError(f"Kernel sslsniff exited early: {command_text(command)}")
    pids = process_pids("sslsniff")
    if len(pids) != 1:
        raise RuntimeError(f"Expected one sslsniff process, found {pids}")
    return process, pids[0]


def start_bpftime_reader(case_dir: Path) -> tuple[subprocess.Popen[bytes], int]:
    environment = os.environ.copy()
    environment["LD_PRELOAD"] = str(SYSCALL_SERVER)
    environment["BPFTIME_LOG_OUTPUT"] = str(case_dir / "runtime.log")
    environment["SPDLOG_LEVEL"] = "info"
    stderr = (case_dir / "sslsniff.stderr").open("wb")
    process = subprocess.Popen(
        [str(SSLSNIFF)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError("BPFtime sslsniff exited early")
    return process, process.pid


def stop_process(process: subprocess.Popen[object] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def instructions_per_request(perf: dict[str, float], denominator: float) -> dict[str, float]:
    required = ("instructions:u", "instructions:k")
    missing = [event for event in required if event not in perf]
    if missing:
        raise RuntimeError(f"Missing perf instruction counters: {missing}; parsed={perf}")
    user = perf["instructions:u"] / denominator
    kernel = perf["instructions:k"] / denominator
    return {"user": user, "kernel": kernel, "total": user + kernel}


def run_case(
    *,
    mode: str,
    round_number: int,
    output: Path,
    perf: str,
    wrk_seconds: int,
    perf_seconds: int,
    connections: int,
) -> dict[str, object]:
    case_dir = output / f"round{round_number:02d}-{mode}"
    case_dir.mkdir(parents=True)
    cleanup()
    reader: subprocess.Popen[bytes] | None = None
    nginx: subprocess.Popen[bytes] | None = None
    reader_pid: int | None = None
    try:
        if mode == "bpftime":
            reader, reader_pid = start_bpftime_reader(case_dir)
            nginx, nginx_worker_pid = start_nginx(case_dir, bpftime=True)
        else:
            nginx, nginx_worker_pid = start_nginx(case_dir, bpftime=False)
            if mode == "kernel-global":
                reader, reader_pid = start_kernel_reader(case_dir, None)
            elif mode == "kernel-nginx-only":
                reader, reader_pid = start_kernel_reader(case_dir, nginx_worker_pid)

        wrk_command = [
            "wrk",
            URL,
            "-c",
            str(connections),
            "-d",
            f"{wrk_seconds}s",
        ]
        wrk = subprocess.Popen(
            wrk_command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)
        perf_processes: dict[str, subprocess.Popen[str]] = {
            "nginx": launch_perf(
                perf, nginx_worker_pid, case_dir / "perf-nginx.csv", perf_seconds
            ),
            "wrk": launch_perf(perf, wrk.pid, case_dir / "perf-wrk.csv", perf_seconds),
        }
        if reader_pid is not None:
            perf_processes["sslsniff"] = launch_perf(
                perf, reader_pid, case_dir / "perf-sslsniff.csv", perf_seconds
            )

        perf_errors: dict[str, str] = {}
        for role, process in perf_processes.items():
            _, stderr = process.communicate(timeout=perf_seconds + 10)
            if process.returncode != 0:
                perf_errors[role] = stderr
        wrk_stdout, wrk_stderr = wrk.communicate(timeout=wrk_seconds + 10)
        (case_dir / "wrk.stdout").write_text(wrk_stdout)
        (case_dir / "wrk.stderr").write_text(wrk_stderr)
        if wrk.returncode != 0:
            raise RuntimeError(f"wrk failed ({wrk.returncode}): {wrk_stderr}")
        if perf_errors:
            raise RuntimeError(f"perf stat failed: {perf_errors}")

        rps = parse_wrk(wrk_stdout)
        denominator = rps * perf_seconds
        nginx_perf = parse_perf(case_dir / "perf-nginx.csv")
        wrk_perf = parse_perf(case_dir / "perf-wrk.csv")
        role_costs: dict[str, object] = {
            "nginx": {
                "perf": nginx_perf,
                "instructions_per_request": instructions_per_request(
                    nginx_perf, denominator
                ),
            },
            "wrk": {
                "perf": wrk_perf,
                "instructions_per_request": instructions_per_request(
                    wrk_perf, denominator
                ),
            },
        }
        if reader_pid is not None:
            reader_perf = parse_perf(case_dir / "perf-sslsniff.csv")
            role_costs["sslsniff"] = {
                "perf": reader_perf,
                "instructions_per_request": instructions_per_request(
                    reader_perf, denominator
                ),
            }

        result: dict[str, object] = {
            "round": round_number,
            "mode": mode,
            "requests_per_sec": rps,
            "wrk_seconds": wrk_seconds,
            "perf_seconds": perf_seconds,
            "connections": connections,
            "pids": {
                "nginx_master": nginx.pid,
                "nginx_worker": nginx_worker_pid,
                "sslsniff": reader_pid,
                "wrk": wrk.pid,
            },
            "roles": role_costs,
        }
        (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        stop_process(reader)
        stop_process(nginx)
        cleanup()


def mean(values: list[float]) -> float:
    return statistics.mean(values)


def stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def metric(result: dict[str, object], role: str, field: str) -> float:
    roles = result["roles"]
    assert isinstance(roles, dict)
    role_data = roles[role]
    assert isinstance(role_data, dict)
    instructions = role_data["instructions_per_request"]
    assert isinstance(instructions, dict)
    return float(instructions[field])


def summarize(results: list[dict[str, object]], output: Path) -> dict[str, object]:
    groups: dict[str, object] = {}
    for mode in MODES:
        samples = [result for result in results if result["mode"] == mode]
        if not samples:
            continue
        values: dict[str, list[float]] = {
            "rps": [float(result["requests_per_sec"]) for result in samples],
            "nginx_user_insn_per_request": [metric(result, "nginx", "user") for result in samples],
            "nginx_kernel_insn_per_request": [metric(result, "nginx", "kernel") for result in samples],
            "nginx_total_insn_per_request": [metric(result, "nginx", "total") for result in samples],
        }
        if mode != "baseline":
            values["reader_total_insn_per_request"] = [
                metric(result, "sslsniff", "total") for result in samples
            ]
        groups[mode] = {
            "n": len(samples),
            **{
                name: {"mean": mean(metric_values), "stdev": stdev(metric_values)}
                for name, metric_values in values.items()
            },
        }

    by_key = {(int(result["round"]), str(result["mode"])): result for result in results}
    overhead_rows: list[dict[str, object]] = []
    for round_number in sorted({int(result["round"]) for result in results}):
        baseline = by_key[(round_number, "baseline")]
        baseline_nginx = metric(baseline, "nginx", "total")
        for mode in MODES[1:]:
            traced = by_key[(round_number, mode)]
            nginx_delta = metric(traced, "nginx", "total") - baseline_nginx
            reader = metric(traced, "sslsniff", "total")
            overhead_rows.append(
                {
                    "round": round_number,
                    "mode": mode,
                    "nginx_delta_instructions_per_request": nginx_delta,
                    "reader_instructions_per_request": reader,
                    "attributed_instructions_per_request": nginx_delta + reader,
                }
            )

    overhead: dict[str, object] = {}
    for mode in MODES[1:]:
        samples = [row for row in overhead_rows if row["mode"] == mode]
        overhead[mode] = {"n": len(samples)}
        for name in (
            "nginx_delta_instructions_per_request",
            "reader_instructions_per_request",
            "attributed_instructions_per_request",
        ):
            values = [float(row[name]) for row in samples]
            assert isinstance(overhead[mode], dict)
            overhead[mode][name] = {"mean": mean(values), "stdev": stdev(values)}

    bpftime_delta = overhead["bpftime"]["nginx_delta_instructions_per_request"]["mean"]
    ratios = {}
    for kernel_mode in ("kernel-global", "kernel-nginx-only"):
        kernel_delta = overhead[kernel_mode]["nginx_delta_instructions_per_request"]["mean"]
        ratios[kernel_mode] = bpftime_delta / kernel_delta

    summary = {
        "groups": groups,
        "overhead": overhead,
        "overhead_rows": overhead_rows,
        "bpftime_nginx_delta_ratio": ratios,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lines = [
        "# x64 ssl-nginx nginx-path perf accounting",
        "",
        "## Result",
        "",
        "Three short 16-byte rounds. Hardware counters are attached to the nginx worker and sslsniff reader separately.",
        "",
        "| Mode | N | RPS | nginx total insn/request | reader insn/request |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        group = groups[mode]
        reader = (
            "—"
            if mode == "baseline"
            else f"{group['reader_total_insn_per_request']['mean']:.1f}"
        )
        lines.append(
            f"| {mode} | {group['n']} | {group['rps']['mean']:.2f} "
            f"| {group['nginx_total_insn_per_request']['mean']:.1f} | {reader} |"
        )
    lines += [
        "",
        "## Same-round attributed overhead",
        "",
        "`nginx delta = traced nginx instructions/request - same-round baseline nginx instructions/request`",
        "",
        "| Mode | nginx delta insn/request | reader insn/request | attributed total insn/request |",
        "|---|---:|---:|---:|",
    ]
    for mode in MODES[1:]:
        item = overhead[mode]
        lines.append(
            f"| {mode} | {item['nginx_delta_instructions_per_request']['mean']:.1f} "
            f"| {item['reader_instructions_per_request']['mean']:.1f} "
            f"| {item['attributed_instructions_per_request']['mean']:.1f} |"
        )
    lines += [
        "",
        f"BPFtime/kernel-global nginx-delta ratio: **{ratios['kernel-global']:.3f}x**.",
        "",
        f"BPFtime/kernel-nginx-only nginx-delta ratio: **{ratios['kernel-nginx-only']:.3f}x**.",
        "",
        "## Interpretation limits",
        "",
        "- This is a short path-location test, not a stable throughput benchmark.",
        "- RPS is taken from the full wrk window; counters cover the inner perf window.",
        "- kernel-global matches the original benchmark's attach scope; kernel-nginx-only removes wrk-side probe events.",
        "- The attributed total is process-level accounting, not exact call-path decomposition.",
    ]
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--payload-bytes", type=int, default=16)
    parser.add_argument("--wrk-seconds", type=int, default=8)
    parser.add_argument("--perf-seconds", type=int, default=5)
    parser.add_argument("--connections", type=int, default=100)
    parser.add_argument("--perf", default=os.environ.get("PERF", "perf"))
    args = parser.parse_args()
    if args.rounds < 1 or args.payload_bytes < 1:
        raise SystemExit("rounds and payload-bytes must be positive")
    if not 0 < args.perf_seconds < args.wrk_seconds:
        raise SystemExit("perf-seconds must be positive and shorter than wrk-seconds")

    require_paths()
    args.output.mkdir(parents=True, exist_ok=True)
    INDEX_HTML.write_bytes(b"x" * args.payload_bytes)
    metadata = {
        "root": str(ROOT),
        "git_head": run(["git", "rev-parse", "HEAD"], capture_output=True).stdout.strip(),
        "rounds": args.rounds,
        "payload_bytes": args.payload_bytes,
        "wrk_seconds": args.wrk_seconds,
        "perf_seconds": args.perf_seconds,
        "connections": args.connections,
        "perf": args.perf,
        "perf_events": PERF_EVENTS,
        "modes": MODES,
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    results: list[dict[str, object]] = []
    try:
        for round_number in range(1, args.rounds + 1):
            order = list(MODES if round_number % 2 else reversed(MODES))
            print(f"round {round_number}: {', '.join(order)}", flush=True)
            for mode in order:
                print(f"running round {round_number} {mode}", flush=True)
                result = run_case(
                    mode=mode,
                    round_number=round_number,
                    output=args.output,
                    perf=args.perf,
                    wrk_seconds=args.wrk_seconds,
                    perf_seconds=args.perf_seconds,
                    connections=args.connections,
                )
                results.append(result)
                print(
                    f"{mode}: {result['requests_per_sec']:.2f} requests/sec",
                    flush=True,
                )
    finally:
        cleanup()
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    summary = summarize(results, args.output)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
