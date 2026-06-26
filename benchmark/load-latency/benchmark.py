#!/usr/bin/env python3
import subprocess
import os
import time
import re
import signal
import platform
from pathlib import Path

# Paths relative to the script location
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
BPFTIME_BIN = Path.home() / ".bpftime" / "bpftime"
BPFTIMETOOL = PROJECT_ROOT / "build/tools/bpftimetool/bpftimetool"
UPROBE_SERVER = PROJECT_ROOT / "example/malloc/malloc"
VICTIM_BIN = PROJECT_ROOT / "example/malloc/victim"
RESULTS_FILE = SCRIPT_DIR / "results.md"

def cleanup():
    """Clean up any leftover processes and shared memory."""
    print("Performing cleanup...")
    # Kill any running uprobe or victim processes
    subprocess.run(["sudo", "pkill", "-9", "-x", "uprobe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-9", "-x", "victim"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-9", "-x", "malloc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-9", "-x", "bpftime"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Clear shared memory
    if BPFTIMETOOL.exists():
        subprocess.run(["sudo", str(BPFTIMETOOL), "remove"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

def run_uprobe_server():
    """Start the uprobe server in the background."""
    print("Starting uprobe server...")
    cmd = ["sudo", "env", "BPFTIME_LOG_OUTPUT=console", str(BPFTIME_BIN), "-i", "/home/y1/.bpftime", "load", str(UPROBE_SERVER)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2) # Give server time to initialize
    return proc

def measure_start_latency():
    """Measure load latency via 'bpftime start' (LD_PRELOAD launch)."""
    print("\n--- Measuring Launch Latency (bpftime start) ---")
    
    # 1. Measure baseline
    cmd_base = [str(VICTIM_BIN)]
    start_base = time.perf_counter()
    proc_base = subprocess.Popen(cmd_base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc_base.stdout:
        if "victim_started" in line:
            break
    end_base = time.perf_counter()
    proc_base.terminate()
    proc_base.wait()
    baseline_ms = (end_base - start_base) * 1000.0
    print(f"Baseline wall clock time: {baseline_ms:.2f} ms")

    # 2. Measure bpftime start
    env = os.environ.copy()
    env["BPFTIME_LOG_OUTPUT"] = "console"
    cmd = ["sudo", "env", "BPFTIME_LOG_OUTPUT=console", str(BPFTIME_BIN), "-i", "/home/y1/.bpftime", "start", str(VICTIM_BIN)]
    
    start_bpftime = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    
    found = False
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        if "victim_started" in line:
            found = True
            break
            
    end_bpftime = time.perf_counter()
    proc.terminate()
    proc.wait()
    
    if found:
        bpftime_ms = (end_bpftime - start_bpftime) * 1000.0
        net_latency_ms = bpftime_ms - baseline_ms
        print(f"BPFtime start wall clock time: {bpftime_ms:.2f} ms")
        print(f"Net load latency: {net_latency_ms:.2f} ms")
        return net_latency_ms
    else:
        print("Failed to detect victim_started")
        return None

def measure_attach_latency():
    """Measure load latency via 'bpftime attach' (dynamic injection)."""
    print("\n--- Measuring Injection Latency (bpftime attach) ---")
    
    print("Starting victim process...")
    victim_proc = subprocess.Popen([str(VICTIM_BIN)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    
    pid = victim_proc.pid
    print(f"Victim PID: {pid}")
    
    cmd = ["sudo", str(BPFTIME_BIN), "-i", "/home/y1/.bpftime", "attach", str(pid)]
    
    start_time = time.perf_counter()
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end_time = time.perf_counter()
    
    attach_duration_ms = (end_time - start_time) * 1000.0
    print(f"Attach command output: {res.stdout.strip()}")
    if res.stderr:
        print(f"Attach command stderr: {res.stderr.strip()}")
        
    print(f"Injection Attach Latency: {attach_duration_ms:.2f} ms")
    
    victim_proc.terminate()
    victim_proc.wait()
    
    return attach_duration_ms

def main():
    cleanup()
    
    server_proc = run_uprobe_server()
    
    launch_latency = None
    try:
        launch_runs = []
        for i in range(10):
            print(f"\n--- Run {i+1}/10 for bpftime start ---")
            lat = measure_start_latency()
            if lat is not None:
                launch_runs.append(lat)
            time.sleep(1)
        if launch_runs:
            launch_latency = sum(launch_runs) / len(launch_runs)
            print(f"\nAverage Launch Latency: {launch_latency:.2f} ms")
    except Exception as e:
        print(f"Error measuring launch latency: {e}")
        
    cleanup()
    
    server_proc = run_uprobe_server()
    
    attach_latency = None
    try:
        attach_runs = []
        for i in range(10):
            print(f"\n--- Run {i+1}/10 for bpftime attach ---")
            lat = measure_attach_latency()
            if lat:
                attach_runs.append(lat)
            time.sleep(1)
        if attach_runs:
            attach_latency = sum(attach_runs) / len(attach_runs)
            print(f"\nAverage Attach Latency: {attach_latency:.2f} ms")
    except Exception as e:
        print(f"Error measuring attach latency: {e}")
        
    cleanup()
    
    print(f"\nWriting results to {RESULTS_FILE}...")
    
    markdown = [
        "# BPFtime Part 3: Load Latency Results",
        "",
        f"*Generated on {time.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
        "## Environment",
        f"- **OS:** {platform.system()} {platform.release()}",
        f"- **Python:** {platform.python_version()}",
        "",
        "## Performance Results",
        "",
        "| Load Method | Latency (ms) | Description |",
        "| :--- | :--- | :--- |"
    ]
    
    if launch_latency is not None:
        if launch_latency == 0.0:
            markdown.append("| **bpftime start** (LD_PRELOAD launch) | < 1 ms | Measure wall-clock time from launch to main execution (excluding baseline) |")
        else:
            markdown.append(f"| **bpftime start** (LD_PRELOAD launch) | {launch_latency:.2f} ms | Measure wall-clock time from launch to main execution (excluding baseline) |")
    else:
        markdown.append("| **bpftime start** (LD_PRELOAD launch) | N/A | Failed to capture start latency |")
        
    if attach_latency:
        markdown.append(f"| **bpftime attach** (Frida injection) | {attach_latency:.2f} ms | Measure wall-clock time of the attach process injection |")
    else:
        markdown.append("| **bpftime attach** (Frida injection) | N/A | Injection failed or timed out |")
        
    markdown.extend([
        "",
        "## Conclusion",
        "- **LD_PRELOAD launch** (`bpftime start`) is extremely fast because it occurs directly during process initialization.",
        "- **Frida dynamic injection** (`bpftime attach`) takes slightly longer (involving process attachment, thread creation, and remote injection) but allows attaching to already running processes without restarting them."
    ])
    
    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(markdown))
        
    print("Done!")

if __name__ == "__main__":
    main()
