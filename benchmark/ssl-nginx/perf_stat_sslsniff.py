#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime


SIZES = {
    "16b": 16,
    "1kb": 1024,
    "2kb": 2 * 1024,
    "4kb": 4 * 1024,
    "16kb": 16 * 1024,
    "32kb": 32 * 1024,
    "64kb": 64 * 1024,
    "128kb": 128 * 1024,
    "256kb": 256 * 1024,
    "384kb": 384 * 1024,
    "512kb": 512 * 1024,
    "640kb": 640 * 1024,
    "768kb": 768 * 1024,
    "896kb": 896 * 1024,
    "1024kb": 1024 * 1024,
}

OUTPUT_DIR = "benchmark/ssl-nginx"
INDEX_HTML_PATH = "benchmark/ssl-nginx/index.html"
NGINX_CONF = "benchmark/ssl-nginx/nginx.conf"
TEST_URL = "https://127.0.0.1:4043/index.html"
SSLSNIFF_PATH = "example/tracing/sslsniff/sslsniff"
AGENT_PATH = "build/runtime/agent/libbpftime-agent.so"
SYSCALL_SERVER_PATH = "build/runtime/syscall-server/libbpftime-syscall-server.so"

PERF_EVENTS = os.environ.get(
    "SSL_NGINX_PERF_EVENTS",
    "instructions,cycles,context-switches,cpu-migrations,cache-misses",
)
PERF_REPEAT = int(os.environ.get("SSL_NGINX_PERF_REPEAT", "3"))
WRK_CONNECTIONS = os.environ.get("SSL_NGINX_WRK_CONNECTIONS", "100")
WRK_DURATION = os.environ.get("SSL_NGINX_WRK_DURATION", "5")
WRK_TIMEOUT = int(os.environ.get("SSL_NGINX_WRK_TIMEOUT", "15"))
READY_TIMEOUT = int(os.environ.get("SSL_NGINX_READY_TIMEOUT", "10"))
SSLSNIFF_ARGS = shlex.split(os.environ.get("SSL_NGINX_SSLSNIFF_ARGS", ""))
BENCH_ORDER = [
    item.strip()
    for item in os.environ.get("SSL_NGINX_BENCH_ORDER", "kernel,bpftime").split(",")
    if item.strip()
]


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def generate_test_file(size_name, size_bytes):
    html_start = "<html>"
    html_end = "</html>"
    content_size = max(0, size_bytes - len(html_start) - len(html_end))
    with open(INDEX_HTML_PATH, "w") as f:
        f.write(html_start + ("X" * content_size) + html_end)
    actual_size = os.path.getsize(INDEX_HTML_PATH)
    log(f"Generated {size_name} payload at {INDEX_HTML_PATH} ({actual_size} bytes)")


def requested_sizes():
    raw = os.environ.get("SSL_NGINX_SIZES", "1kb")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [name for name in names if name not in SIZES]
    if unknown:
        raise SystemExit(f"Unknown SSL_NGINX_SIZES entries: {', '.join(unknown)}")
    return [(name, SIZES[name]) for name in names]


def cleanup_processes():
    subprocess.run(["sudo", "pkill", "-f", "example/tracing/sslsniff/sslsniff"], stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-f", "nginx: master process nginx"], stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-f", "wrk https://127.0.0.1:4043/index.html"], stderr=subprocess.DEVNULL)
    time.sleep(1)


def terminate_process(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def wait_for_nginx_ready(proc, label):
    deadline = time.time() + READY_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            log(f"{label} exited before readiness; rc={proc.returncode}")
            if stdout:
                log(f"{label} stdout: {stdout.decode(errors='replace')}")
            if stderr:
                log(f"{label} stderr: {stderr.decode(errors='replace')}")
            return False
        try:
            check = subprocess.run(
                ["curl", "-k", "-s", "-o", "/dev/null", "-w", "%{http_code}", TEST_URL],
                capture_output=True,
                text=True,
                timeout=3,
            )
            last_status = check.stdout.strip()
            if last_status == "200":
                return True
        except Exception as exc:
            last_status = str(exc)
        time.sleep(0.5)
    log(f"{label} readiness failed; last status/error: {last_status}")
    return False


def parse_wrk_output(text):
    match = re.search(r"Requests/sec:\s+(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def parse_perf_csv(text):
    counters = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        raw_value = parts[0].strip()
        event = parts[2].strip()
        if not event:
            continue
        normalized = raw_value.replace(",", "")
        try:
            value = float(normalized)
        except ValueError:
            value = None
        counters[event] = {
            "value": value,
            "raw": raw_value,
        }
    return counters


def start_nginx(mode):
    abs_nginx_conf = os.path.abspath(NGINX_CONF)
    abs_nginx_dir = os.path.dirname(abs_nginx_conf)
    cmd = ["nginx", "-c", abs_nginx_conf, "-p", abs_nginx_dir]
    env = os.environ.copy()
    if mode == "bpftime":
        env["LD_PRELOAD"] = AGENT_PATH
    log(f"Starting {mode} nginx")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    if not wait_for_nginx_ready(proc, f"{mode} nginx"):
        terminate_process(proc)
        raise RuntimeError(f"{mode} nginx did not become ready")
    return proc


def start_sslsniff(mode):
    cmd = [SSLSNIFF_PATH, *SSLSNIFF_ARGS]
    env = os.environ.copy()
    if mode == "kernel":
        cmd = ["sudo", *cmd]
    elif mode == "bpftime":
        env["LD_PRELOAD"] = SYSCALL_SERVER_PATH
    else:
        raise ValueError(f"unknown mode: {mode}")
    log(f"Starting {mode} sslsniff reader")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"{mode} sslsniff exited early with rc={proc.returncode}")
    return proc


def process_table():
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm=,args="],
        capture_output=True,
        text=True,
        check=True,
    )
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, comm, args = parts
        processes.append({
            "pid": int(pid),
            "ppid": int(ppid),
            "comm": comm,
            "args": args,
        })
    return processes


def find_descendant_sslsniff_pid(root_pid):
    processes = process_table()
    children_by_parent = {}
    for proc in processes:
        children_by_parent.setdefault(proc["ppid"], []).append(proc)

    stack = list(children_by_parent.get(root_pid, []))
    while stack:
        proc = stack.pop(0)
        if proc["comm"] == "sslsniff" or SSLSNIFF_PATH in proc["args"]:
            return proc["pid"]
        stack.extend(children_by_parent.get(proc["pid"], []))
    return None


def wait_for_perf_target_pid(mode, sslsniff_proc):
    if mode == "bpftime":
        return sslsniff_proc.pid

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        target_pid = find_descendant_sslsniff_pid(sslsniff_proc.pid)
        if target_pid is not None:
            return target_pid
        time.sleep(0.2)
    raise RuntimeError(f"could not find kernel sslsniff child process for sudo pid={sslsniff_proc.pid}")


def run_perf_wrk(sslsniff_pid):
    wrk_cmd = ["wrk", TEST_URL, "-c", WRK_CONNECTIONS, "-d", WRK_DURATION]
    perf_cmd = [
        "sudo",
        "perf",
        "stat",
        "-x",
        ",",
        "-e",
        PERF_EVENTS,
        "-p",
        str(sslsniff_pid),
        "--",
        *wrk_cmd,
    ]
    log(f"Running perf stat on sslsniff pid={sslsniff_pid}: {' '.join(wrk_cmd)}")
    result = subprocess.run(perf_cmd, capture_output=True, text=True, timeout=WRK_TIMEOUT)
    req_per_sec = parse_wrk_output(result.stdout)
    counters = parse_perf_csv(result.stderr)
    return {
        "returncode": result.returncode,
        "requests_per_sec": req_per_sec,
        "perf_counters": counters,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": perf_cmd,
    }


def run_one(mode):
    cleanup_processes()
    nginx_proc = None
    sslsniff_proc = None
    try:
        if mode == "bpftime":
            sslsniff_proc = start_sslsniff(mode)
            nginx_proc = start_nginx(mode)
        else:
            nginx_proc = start_nginx(mode)
            sslsniff_proc = start_sslsniff(mode)
        perf_target_pid = wait_for_perf_target_pid(mode, sslsniff_proc)
        log(f"Using {mode} sslsniff perf target pid={perf_target_pid}")
        return run_perf_wrk(perf_target_pid)
    finally:
        terminate_process(sslsniff_proc)
        terminate_process(nginx_proc)
        cleanup_processes()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for required in [NGINX_CONF, SSLSNIFF_PATH, AGENT_PATH, SYSCALL_SERVER_PATH]:
        if not os.path.exists(required):
            raise SystemExit(f"Required path is missing: {required}")

    all_results = []
    for size_name, size_bytes in requested_sizes():
        generate_test_file(size_name, size_bytes)
        size_result = {
            "size_name": size_name,
            "size_bytes": size_bytes,
            "runs": {},
        }
        for mode in BENCH_ORDER:
            if mode not in ("kernel", "bpftime"):
                raise SystemExit(f"Unsupported SSL_NGINX_BENCH_ORDER entry for perf stat: {mode}")
            mode_runs = []
            for run_idx in range(PERF_REPEAT):
                log(f"=== {size_name} {mode} perf run {run_idx + 1}/{PERF_REPEAT} ===")
                try:
                    run_result = run_one(mode)
                except Exception as exc:
                    log(f"{size_name} {mode} perf run failed: {exc}")
                    run_result = {
                        "returncode": None,
                        "error": str(exc),
                        "requests_per_sec": None,
                        "perf_counters": {},
                    }
                mode_runs.append(run_result)
            size_result["runs"][mode] = mode_runs
        all_results.append(size_result)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(OUTPUT_DIR, f"perf_stat_sslsniff_{timestamp}.json")
    with open(output_path, "w") as f:
        json.dump(
            {
                "config": {
                    "perf_events": PERF_EVENTS,
                    "perf_repeat": PERF_REPEAT,
                    "wrk_connections": WRK_CONNECTIONS,
                    "wrk_duration": WRK_DURATION,
                    "wrk_timeout": WRK_TIMEOUT,
                    "bench_order": BENCH_ORDER,
                    "sslsniff_args": SSLSNIFF_ARGS,
                },
                "results": all_results,
            },
            f,
            indent=2,
        )
    log(f"Perf stat results saved to {output_path}")


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_processes()
