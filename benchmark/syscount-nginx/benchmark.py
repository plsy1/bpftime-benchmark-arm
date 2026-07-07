#!/usr/bin/env python3
import subprocess
import re
import os
import time
import json
import statistics
import signal
import sys
import traceback
from datetime import datetime
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import matplotlib as mpl

# Configuration
NUM_RUNS = int(os.environ.get("SYSCOUNT_NGINX_NUM_RUNS", "10"))
NGINX_PORT = os.environ.get("SYSCOUNT_NGINX_PORT", "1801")
WRK_CONNECTIONS = os.environ.get("SYSCOUNT_NGINX_WRK_CONNECTIONS", "10")
WRK_DURATION = os.environ.get("SYSCOUNT_NGINX_WRK_DURATION", "10")
WRK_TIMEOUT = int(os.environ.get("SYSCOUNT_NGINX_WRK_TIMEOUT", "15"))
SYSCOUNT_DURATION = os.environ.get("SYSCOUNT_NGINX_DURATION", "20")
SYSCOUNT_TIMEOUT = int(os.environ.get("SYSCOUNT_NGINX_TIMEOUT", str(int(SYSCOUNT_DURATION) + 5)))
SYSCOUNT_STARTUP_DELAY = float(os.environ.get("SYSCOUNT_NGINX_STARTUP_DELAY", "2"))
ALLOW_MISSING_USERBPF = os.environ.get("SYSCOUNT_NGINX_ALLOW_MISSING_USERBPF", "").lower() in ("1", "true", "yes", "on")
WRK_CMD = ["wrk", f"http://127.0.0.1:{NGINX_PORT}/index.html", "-c", WRK_CONNECTIONS, "-d", WRK_DURATION]
NGINX_BIN = os.environ.get("SYSCOUNT_NGINX_BIN", "nginx")
NGINX_CMD = [NGINX_BIN, "-c", "nginx.conf", "-p", "benchmark/syscount-nginx"]
TEST_URL = f"http://127.0.0.1:{NGINX_PORT}/index.html"
SYSCOUNT_PATH = "example/tracing/syscount/syscount"
AGENT_PATH = "build/runtime/agent/libbpftime-agent.so"
SYSCALL_SERVER_PATH = "build/runtime/syscall-server/libbpftime-syscall-server.so"
TRACE_LOG_ROOT = Path(os.environ.get(
    "SYSCOUNT_NGINX_TRACE_LOG_DIR",
    f"benchmark/syscount-nginx/trace_logs/{datetime.now().strftime('%Y%m%d_%H%M%S')}",
))

# Result storage
results = {
    "native": [],               # No tracing
    "kernel_targeted": [],      # Kernel syscount targeting nginx pid
    "kernel_untargeted": [],    # Kernel syscount not targeting nginx
    "userbpf_targeted": [],     # Userspace syscount targeting nginx pid
    "userbpf_untargeted": [],   # Userspace syscount not targeting nginx
}
run_stats = {
    name: {
        "target_runs": NUM_RUNS,
        "valid_runs": 0,
        "failed_attempts": 0,
        "trace_failures": 0,
        "wrk_failures": 0,
        "parse_failures": 0,
        "timeouts": 0,
        "exceptions": 0,
        "skipped": False,
        "skip_reason": "",
    }
    for name in results
}
trace_observability = {
    "kernel_targeted": [],
    "kernel_untargeted": [],
    "userbpf_targeted": [],
    "userbpf_untargeted": [],
}

# Define a signal handler to prevent unexpected termination
def signal_handler(sig, frame):
    debug_print(f"Received signal {sig}, gracefully exiting...")
    sys.exit(0)

# Register the signal handler for common signals
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def debug_print(message):
    """Print debug messages with timestamp"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[DEBUG {timestamp}] {message}")

def mark_valid(test_name):
    run_stats[test_name]["valid_runs"] += 1

def mark_failed(test_name, reason):
    run_stats[test_name]["failed_attempts"] += 1
    if reason in run_stats[test_name]:
        run_stats[test_name][reason] += 1

def mark_skipped(test_name, reason):
    run_stats[test_name]["skipped"] = True
    run_stats[test_name]["skip_reason"] = reason

def open_trace_logs(test_name, run_index):
    TRACE_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    suffix = f"run{run_index + 1:02d}"
    stdout_path = TRACE_LOG_ROOT / f"{test_name}_{suffix}.stdout.log"
    stderr_path = TRACE_LOG_ROOT / f"{test_name}_{suffix}.stderr.log"
    stdout_file = stdout_path.open("w")
    stderr_file = stderr_path.open("w")
    return stdout_path, stderr_path, stdout_file, stderr_file

def summarize_syscount_log(test_name, run_index, stdout_path, stderr_path, returncode, require_counts):
    stdout_text = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    stderr_text = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    hard_error_markers = [
        marker for marker in (
            "failed to open BPF object",
            "failed to load BPF object",
            "failed to attach sys_exit program",
            "failed to attach sys_enter programs",
            "libbpf: failed",
            "ERROR:",
            "Error:",
        )
        if marker in stdout_text or marker in stderr_text
    ]
    count_rows = []
    in_count_table = False
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_count_table = False
            continue
        if "COUNT" in stripped and ("SYSCALL" in stripped or "PID" in stripped):
            in_count_table = True
            continue
        if in_count_table:
            parts = stripped.split()
            if parts and parts[-1].isdigit():
                count_rows.append(stripped)

    started = "Tracing syscalls" in stdout_text
    trace_success = started and not hard_error_markers and (not require_counts or bool(count_rows))
    summary = {
        "run": run_index + 1,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "returncode": returncode,
        "started": started,
        "require_counts": require_counts,
        "count_rows": len(count_rows),
        "trace_success": trace_success,
        "error_markers": hard_error_markers,
        "stdout_bytes": stdout_path.stat().st_size if stdout_path.exists() else 0,
        "stderr_bytes": stderr_path.stat().st_size if stderr_path.exists() else 0,
    }
    trace_observability[test_name].append(summary)
    debug_print(
        f"{test_name} run {run_index + 1}: trace_success={trace_success} "
        f"started={started} count_rows={len(count_rows)} returncode={returncode} "
        f"stdout={stdout_path} stderr={stderr_path}"
    )
    return summary

def terminate_and_summarize_syscount(test_name, run_index, proc, trace_files, require_counts):
    stdout_path, stderr_path, stdout_file, stderr_file = trace_files
    returncode = None
    if proc is not None:
        try:
            proc.wait(timeout=SYSCOUNT_TIMEOUT)
        except subprocess.TimeoutExpired:
            mark_failed(test_name, "timeouts")
            debug_print("syscount didn't finish within expected time, terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        returncode = proc.poll()
    stdout_file.close()
    stderr_file.close()
    return summarize_syscount_log(test_name, run_index, stdout_path, stderr_path, returncode, require_counts)

def remove_access_log():
    """Remove the nginx access log file"""
    log_path = os.path.join("benchmark", "syscount-nginx", "access.log")
    abs_log_path = os.path.abspath(log_path)
    
    debug_print(f"Removing access log: {abs_log_path}")
    try:
        if os.path.exists(abs_log_path):
            os.remove(abs_log_path)
            debug_print("Access log removed successfully")
        else:
            debug_print("Access log not found (this might be normal on first run)")
    except Exception as e:
        debug_print(f"Error removing access log: {e}")

def prepare_nginx_prefix(nginx_dir):
    """Create directories expected by source-built nginx when -p is used."""
    logs_dir = os.path.join(nginx_dir, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        debug_print(f"Ensured nginx logs directory exists: {logs_dir}")
    except Exception as e:
        debug_print(f"Error creating nginx logs directory {logs_dir}: {e}")

def check_file_exists(path):
    """Check if a file exists and print its absolute path"""
    abs_path = os.path.abspath(path)
    exists = os.path.exists(abs_path)
    debug_print(f"Checking file: {abs_path} - {'EXISTS' if exists else 'NOT FOUND'}")
    return exists

def check_command_exists(cmd):
    """Check if a command exists in PATH"""
    try:
        if os.path.sep in cmd:
            exists = os.path.isfile(cmd) and os.access(cmd, os.X_OK)
            debug_print(f"Checking command path: {cmd} - {'EXISTS' if exists else 'NOT FOUND'}")
            return exists

        result = subprocess.run(["which", cmd], capture_output=True, text=True)
        exists = result.returncode == 0
        debug_print(f"Checking command: {cmd} - {'EXISTS' if exists else 'NOT FOUND'}")
        
        if exists:
            debug_print(f"  Path: {result.stdout.strip()}")
        else:
            # For nginx, check common locations
            if cmd == NGINX_BIN:
                common_paths = [
                    "/usr/sbin/nginx", 
                    "/usr/local/sbin/nginx",
                    "/usr/local/bin/nginx",
                    "/opt/nginx/sbin/nginx"
                ]
                for path in common_paths:
                    if os.path.exists(path):
                        debug_print(f"Found {cmd} at {path}")
                        # Update global nginx command
                        global NGINX_CMD
                        NGINX_CMD[0] = path
                        return True
                
                debug_print("Could not find nginx in common locations")
        
        return exists
    except Exception as e:
        debug_print(f"Error checking command {cmd}: {e}")
        return False

def cleanup_processes():
    """Kill any running nginx or syscount processes"""
    try:
        debug_print("Cleaning up processes...")
        
        # Get the PIDs of nginx and syscount processes
        nginx_pids = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, text=True).stdout.strip().split()
        syscount_pids = subprocess.run(["pgrep", "-x", "syscount"], capture_output=True, text=True).stdout.strip().split()
        
        debug_print(f"Found nginx PIDs: {nginx_pids}")
        debug_print(f"Found syscount PIDs: {syscount_pids}")
        
        # Kill nginx processes by PID
        for pid in nginx_pids:
            try:
                debug_print(f"Terminating nginx process with PID {pid}")
                subprocess.run(["kill", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error terminating nginx PID {pid}: {e}")
        
        # Kill syscount processes by PID
        for pid in syscount_pids:
            try:
                debug_print(f"Terminating syscount process with PID {pid}")
                subprocess.run(["kill", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error terminating syscount PID {pid}: {e}")
                try:
                    debug_print(f"Trying with sudo...")
                    subprocess.run(["sudo", "kill", pid], stderr=subprocess.DEVNULL, check=False)
                except Exception as e2:
                    debug_print(f"Error with sudo: {e2}")
        
        # Wait for processes to terminate
        time.sleep(1)
        
        # Check if any processes are still running and try forceful termination if needed
        remaining_nginx_pids = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, text=True).stdout.strip().split()
        remaining_syscount_pids = subprocess.run(["pgrep", "-x", "syscount"], capture_output=True, text=True).stdout.strip().split()
        
        debug_print(f"After cleanup: nginx PIDs: {remaining_nginx_pids}, syscount PIDs: {remaining_syscount_pids}")
        
        # Force kill remaining processes
        for pid in remaining_nginx_pids:
            try:
                debug_print(f"Force killing nginx process with PID {pid}")
                subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error force killing nginx PID {pid}: {e}")
        
        for pid in remaining_syscount_pids:
            try:
                debug_print(f"Force killing syscount process with PID {pid}")
                subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error force killing syscount PID {pid}: {e}")
                try:
                    debug_print(f"Trying with sudo...")
                    subprocess.run(["sudo", "kill", "-9", pid], stderr=subprocess.DEVNULL, check=False)
                except Exception as e2:
                    debug_print(f"Error with sudo: {e2}")
    
    except Exception as e:
        debug_print(f"Error during cleanup: {e}")
        traceback.print_exc()

def parse_wrk_output(output):
    """Parse wrk output to extract requests/sec"""
    debug_print(f"Parsing wrk output: {output[:100]}...")  # Print first 100 chars
    match = re.search(r'Requests/sec:\s+(\d+\.\d+)', output)
    if match:
        return float(match.group(1))
    debug_print("Failed to parse wrk output")
    return None

def start_nginx():
    """Start nginx server and return process and PID"""
    cleanup_processes()
    remove_access_log()  # Remove access log before starting nginx
    
    # Check if nginx exists
    check_command_exists(NGINX_BIN)
    
    # Check current directory
    debug_print(f"Current directory: {os.getcwd()}")
    nginx_conf = "benchmark/syscount-nginx/nginx.conf"
    debug_print(f"Checking if nginx.conf exists: {os.path.exists(nginx_conf)}")
    
    if not os.path.exists(nginx_conf):
        debug_print(f"ERROR: {nginx_conf} not found!")
        return None, None
    
    # Start nginx with full path
    abs_nginx_conf = os.path.abspath(nginx_conf)
    abs_nginx_dir = os.path.dirname(abs_nginx_conf)
    prepare_nginx_prefix(abs_nginx_dir)
    modified_nginx_cmd = [NGINX_BIN, "-c", abs_nginx_conf, "-p", abs_nginx_dir]
    debug_print(f"Starting nginx with command: {' '.join(modified_nginx_cmd)}")
    
    try:
        nginx_proc = subprocess.Popen(modified_nginx_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2)  # Give nginx time to start
        
        # Check if nginx started successfully
        if nginx_proc.poll() is not None:
            stdout, stderr = nginx_proc.communicate()
            debug_print(f"nginx failed to start. Exit code: {nginx_proc.returncode}")
            debug_print(f"stdout: {stdout.decode() if stdout else 'None'}")
            debug_print(f"stderr: {stderr.decode() if stderr else 'None'}")
            return None, None
        
        debug_print("nginx started successfully")
        
        # Check if nginx is listening
        try:
            curl_check = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", TEST_URL], 
                                        capture_output=True, text=True, timeout=5)
            debug_print(f"HTTP status code from nginx: {curl_check.stdout}")
            
            # Get nginx PID
            nginx_pid = subprocess.run(["pgrep", "-f", "nginx -c"], capture_output=True, text=True).stdout.strip()
            debug_print(f"nginx PID: {nginx_pid}")
            return nginx_proc, nginx_pid
            
        except Exception as e:
            debug_print(f"Error checking nginx: {e}")
            return nginx_proc, None
    
    except Exception as e:
        debug_print(f"Error starting nginx: {e}")
        traceback.print_exc()
        return None, None

def run_native():
    """Run baseline benchmarks (no tracing)"""
    print("\n=== Running Native Tests (No Tracing) ===")
    
    nginx_proc, nginx_pid = start_nginx()
    if not nginx_proc:
        debug_print("Failed to start nginx, skipping native test")
        return
    
    try:
        for i in range(NUM_RUNS):
            print(f"Run {i+1}/{NUM_RUNS}...")
            debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
            
            # Run wrk
            try:
                result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
                debug_print(f"wrk exit code: {result.returncode}")
                
                if result.returncode != 0:
                    mark_failed("native", "wrk_failures")
                    debug_print(f"wrk failed with exit code {result.returncode}")
                    debug_print(f"stdout: {result.stdout}")
                    debug_print(f"stderr: {result.stderr}")
                    continue
                
                req_per_sec = parse_wrk_output(result.stdout)
                if req_per_sec:
                    results["native"].append(req_per_sec)
                    mark_valid("native")
                    print(f"  Requests/sec: {req_per_sec:.2f}")
                else:
                    mark_failed("native", "parse_failures")
                    debug_print(f"Failed to parse output: {result.stdout}")
            except subprocess.TimeoutExpired:
                mark_failed("native", "timeouts")
                debug_print("wrk command timed out")
            except Exception as e:
                mark_failed("native", "exceptions")
                debug_print(f"Error running wrk: {e}")
                traceback.print_exc()
    
    except Exception as e:
        debug_print(f"Error in native test: {e}")
        traceback.print_exc()
    finally:
        # Cleanup
        if nginx_proc:
            debug_print("Terminating nginx")
            try:
                nginx_proc.terminate()
                nginx_proc.wait(timeout=5)
            except Exception as e:
                debug_print(f"Error terminating nginx: {e}")
                try:
                    nginx_proc.kill()
                except:
                    pass

def run_kernel_syscount(target_pid=None):
    """
    Run kernel syscount benchmarks
    If target_pid is provided, syscount will target that PID
    Otherwise, it will target a non-nginx PID while wrk measures nginx
    """
    test_name = "kernel_targeted" if target_pid else "kernel_untargeted"
    print(f"\n=== Running Kernel syscount Tests ({test_name}) ===")
    
    # Start nginx
    nginx_proc, nginx_pid = start_nginx()
    if not nginx_proc:
        debug_print("Failed to start nginx, skipping kernel syscount test")
        return
    
    # Check if syscount exists
    if not check_file_exists(SYSCOUNT_PATH):
        debug_print(f"Skipping kernel syscount tests: {SYSCOUNT_PATH} not found")
        return
    
    try:
        for i in range(NUM_RUNS):
            print(f"Run {i+1}/{NUM_RUNS}...")

            if not nginx_pid:
                debug_print("nginx PID not found, skipping syscount")
                continue
            
            # Start syscount
            syscount_cmd = ["sudo", SYSCOUNT_PATH, "-d", SYSCOUNT_DURATION]
            if target_pid:
                syscount_cmd.extend(["-p", nginx_pid])
            else:
                # Match the original benchmark design: syscount does not target nginx.
                syscount_cmd.extend(["-p", "1"])
            
            debug_print(f"Starting kernel syscount: {' '.join(syscount_cmd)}")
            syscount_proc = None
            trace_files = None
            req_per_sec = None
            try:
                trace_files = open_trace_logs(test_name, i)
                stdout_path, stderr_path, stdout_file, stderr_file = trace_files
                syscount_proc = subprocess.Popen(
                    syscount_cmd, 
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                time.sleep(SYSCOUNT_STARTUP_DELAY)  # Give syscount time to start
                if syscount_proc.poll() is not None:
                    debug_print(f"kernel syscount exited before wrk. Exit code: {syscount_proc.returncode}")
                    mark_failed(test_name, "trace_failures")
                    continue
                
                # Run wrk
                debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
                result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
                debug_print(f"wrk exit code: {result.returncode}")
                if result.returncode != 0:
                    mark_failed(test_name, "wrk_failures")
                    debug_print(f"wrk failed stdout: {result.stdout}")
                    debug_print(f"wrk failed stderr: {result.stderr}")
                    continue
                req_per_sec = parse_wrk_output(result.stdout)
                
                if not req_per_sec:
                    mark_failed(test_name, "parse_failures")
                    debug_print(f"Failed to parse output: {result.stdout}")
                    
            except subprocess.TimeoutExpired:
                mark_failed(test_name, "timeouts")
                debug_print("wrk command timed out")
            except Exception as e:
                mark_failed(test_name, "exceptions")
                debug_print(f"Error in kernel syscount run: {e}")
                traceback.print_exc()
            finally:
                if trace_files is not None:
                    debug_print("Waiting for syscount to finish...")
                    summary = terminate_and_summarize_syscount(
                        test_name,
                        i,
                        syscount_proc,
                        trace_files,
                        require_counts=bool(target_pid),
                    )
                    if req_per_sec:
                        if summary["trace_success"]:
                            results[test_name].append(req_per_sec)
                            mark_valid(test_name)
                            print(f"  Requests/sec: {req_per_sec:.2f}")
                        else:
                            mark_failed(test_name, "trace_failures")
                            debug_print("kernel syscount trace was not effective; run is not counted")

    except Exception as e:
        debug_print(f"Error in kernel syscount test: {e}")
        traceback.print_exc()
    finally:
        # Cleanup
        debug_print("Terminating nginx")
        try:
            if nginx_proc:
                nginx_proc.terminate()
                nginx_proc.wait(timeout=5)
        except Exception as e:
            debug_print(f"Error terminating nginx: {e}")
            try:
                nginx_proc.kill()
            except:
                pass

def run_userbpf_syscount(target_pid=None):
    """
    Run userspace BPF syscount benchmarks
    If target_pid is provided, syscount will target that PID
    Otherwise, it will target a non-nginx PID while wrk measures nginx
    """
    test_name = "userbpf_targeted" if target_pid else "userbpf_untargeted"
    print(f"\n=== Running UserBPF syscount Tests ({test_name}) ===")
    
    # Check if required files exist
    if not check_file_exists(SYSCOUNT_PATH) or not check_file_exists(AGENT_PATH) or not check_file_exists(SYSCALL_SERVER_PATH):
        debug_print("Skipping userspace BPF syscount tests: required files not found")
        return
    
    for i in range(NUM_RUNS):
        print(f"Run {i+1}/{NUM_RUNS}...")

        nginx_proc = None
        syscount_proc = None
        trace_files = None
        req_per_sec = None
        try:
            # Use the same nginx path approach as baseline
            nginx_conf = "benchmark/syscount-nginx/nginx.conf"
            if not os.path.exists(nginx_conf):
                debug_print(f"ERROR: {nginx_conf} not found!")
                continue
            
            # Start nginx with full path
            abs_nginx_conf = os.path.abspath(nginx_conf)
            abs_nginx_dir = os.path.dirname(abs_nginx_conf)
            prepare_nginx_prefix(abs_nginx_dir)
            modified_nginx_cmd = [NGINX_BIN, "-c", abs_nginx_conf, "-p", abs_nginx_dir]

            env = os.environ.copy()
            nginx_label = "nginx"
            if target_pid:
                nginx_label = "nginx with bpftime"
                env["LD_PRELOAD"] = os.path.abspath(AGENT_PATH)

            debug_print(f"Starting {nginx_label}: {' '.join(modified_nginx_cmd)}")
            nginx_proc = subprocess.Popen(
                modified_nginx_cmd,
                env=env if target_pid else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(2)  # Give nginx time to start

            if nginx_proc.poll() is not None:
                stdout, stderr = nginx_proc.communicate()
                debug_print(f"{nginx_label} failed to start. Exit code: {nginx_proc.returncode}")
                debug_print(f"stdout: {stdout.decode() if stdout else 'None'}")
                debug_print(f"stderr: {stderr.decode() if stderr else 'None'}")
                mark_failed(test_name, "exceptions")
                continue

            nginx_pid = subprocess.run(["pgrep", "-f", "nginx -c"], capture_output=True, text=True).stdout.strip()
            debug_print(f"nginx PID: {nginx_pid}")
            if target_pid and not nginx_pid:
                debug_print("nginx PID not found, skipping syscount")
                mark_failed(test_name, "exceptions")
                continue

            syscall_server_path = os.path.abspath(SYSCALL_SERVER_PATH)
            syscount_path = os.path.abspath(SYSCOUNT_PATH)
            syscount_cmd = [
                "sudo",
                "env",
                f"LD_PRELOAD={syscall_server_path}",
                syscount_path,
                "-d",
                SYSCOUNT_DURATION,
            ]
            if target_pid:
                syscount_cmd.extend(["-p", nginx_pid])
            else:
                # Match the original benchmark design: syscount does not target nginx.
                syscount_cmd.extend(["-p", "1"])
            debug_print(f"Starting syscount with bpftime: {' '.join(syscount_cmd)}")
            trace_files = open_trace_logs(test_name, i)
            stdout_path, stderr_path, stdout_file, stderr_file = trace_files
            syscount_proc = subprocess.Popen(
                syscount_cmd,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            time.sleep(SYSCOUNT_STARTUP_DELAY)  # Give syscount time to start
            if syscount_proc.poll() is not None:
                debug_print(f"userbpf syscount exited before wrk. Exit code: {syscount_proc.returncode}")
                mark_failed(test_name, "trace_failures")
                continue
            
            # Run wrk
            debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
            result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
            debug_print(f"wrk exit code: {result.returncode}")
            if result.returncode != 0:
                mark_failed(test_name, "wrk_failures")
                debug_print(f"wrk failed stdout: {result.stdout}")
                debug_print(f"wrk failed stderr: {result.stderr}")
                continue
            req_per_sec = parse_wrk_output(result.stdout)
            
            if not req_per_sec:
                mark_failed(test_name, "parse_failures")
                debug_print(f"Failed to parse output: {result.stdout}")
                
        except subprocess.TimeoutExpired:
            mark_failed(test_name, "timeouts")
            debug_print("wrk command timed out")
        except Exception as e:
            mark_failed(test_name, "exceptions")
            debug_print(f"Error in userspace BPF syscount run: {e}")
            traceback.print_exc()
        finally:
            # Cleanup
            debug_print("Terminating processes")
            try:
                if nginx_proc is not None:
                    nginx_proc.terminate()
                    nginx_proc.wait(timeout=5)
            except Exception as e:
                debug_print(f"Error terminating processes: {e}")

            if trace_files is not None:
                debug_print("Waiting for syscount to finish...")
                summary = terminate_and_summarize_syscount(
                    test_name,
                    i,
                    syscount_proc,
                    trace_files,
                    require_counts=bool(target_pid),
                )
                if req_per_sec:
                    if summary["trace_success"]:
                        results[test_name].append(req_per_sec)
                        mark_valid(test_name)
                        print(f"  Requests/sec: {req_per_sec:.2f}")
                    else:
                        mark_failed(test_name, "trace_failures")
                        debug_print("userbpf syscount trace was not effective; run is not counted")
            
            cleanup_processes()

def print_statistics():
    """Print benchmark statistics"""
    print("\n=== Benchmark Results ===")
    print(f"Each configuration run {NUM_RUNS} times")
    
    # Dictionary for formatted names for printing
    name_map = {
        "native": "Native (No tracing)",
        "kernel_targeted": "Kernel syscount (targeting nginx)",
        "kernel_untargeted": "Kernel syscount (not targeting nginx)",
        "userbpf_targeted": "UserBPF syscount (targeting nginx)",
        "userbpf_untargeted": "UserBPF syscount (not targeting nginx)"
    }
    
    # For storing avg values for later comparison
    avgs = {}
    
    for test_name, values in results.items():
        stats = run_stats[test_name]
        print(f"\n{name_map.get(test_name, test_name)}:")
        if stats.get("skipped"):
            print(f"  Skipped:               {stats['skip_reason']}")
            continue
        print(f"  Valid runs:            {stats['valid_runs']}/{stats['target_runs']}")
        print(f"  Failed attempts:       {stats['failed_attempts']}")
        breakdown = [
            f"{reason}={stats[reason]}"
            for reason in (
                "trace_failures",
                "wrk_failures",
                "parse_failures",
                "timeouts",
                "exceptions",
            )
            if stats.get(reason, 0)
        ]
        if breakdown:
            print(f"  Failure breakdown:     {', '.join(breakdown)}")
        if not values:
            print("  No valid results")
            continue
            
        avg = statistics.mean(values)
        avgs[test_name] = avg
        median = statistics.median(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0
        min_val = min(values)
        max_val = max(values)
        
        print(f"  Requests/sec (mean):   {avg:.2f}")
        print(f"  Requests/sec (median): {median:.2f}")
        print(f"  Standard deviation:    {stdev:.2f}")
        print(f"  Min:                   {min_val:.2f}")
        print(f"  Max:                   {max_val:.2f}")
        print(f"  All runs:              {[round(x, 2) for x in values]}")
    
    # Compare results
    if "native" in avgs:
        native_avg = avgs["native"]
        print("\n=== Performance Impact Compared to Native ===")
        
        for test_name, avg in avgs.items():
            if test_name != "native":
                impact = ((native_avg - avg) / native_avg) * 100
                print(f"{name_map.get(test_name, test_name)}: {impact:.2f}% decrease")
        
    # Compare userBPF to kernel
    if "kernel_targeted" in avgs and "userbpf_targeted" in avgs:
        kernel_avg = avgs["kernel_targeted"]
        userbpf_avg = avgs["userbpf_targeted"]
        improvement = ((userbpf_avg - kernel_avg) / kernel_avg) * 100
        print(f"\nUserBPF improvement over kernel (targeted): {improvement:.2f}%")
    
    if "kernel_untargeted" in avgs and "userbpf_untargeted" in avgs:
        kernel_avg = avgs["kernel_untargeted"]
        userbpf_avg = avgs["userbpf_untargeted"]
        improvement = ((userbpf_avg - kernel_avg) / kernel_avg) * 100
        print(f"UserBPF improvement over kernel (not targeting nginx): {improvement:.2f}%")

    print("\nTrace observability:")
    for test_name, summaries in trace_observability.items():
        if not summaries:
            print(f"  {test_name}: no trace logs")
            continue
        trace_ok = sum(1 for item in summaries if item["trace_success"])
        count_rows = sum(item["count_rows"] for item in summaries)
        print(
            f"  {test_name}: trace_success={trace_ok}/{len(summaries)} "
            f"count_rows={count_rows} logs={TRACE_LOG_ROOT}"
        )
    
    return avgs

def save_results():
    """Save results to a JSON file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"benchmark/syscount-nginx/benchmark_results_{timestamp}.json"
    
    with open(filename, 'w') as f:
        json.dump({
            "timestamp": timestamp,
            "runs": NUM_RUNS,
            "config": {
                "wrk_cmd": WRK_CMD,
                "nginx_bin": NGINX_BIN,
                "wrk_timeout": WRK_TIMEOUT,
                "syscount_duration": SYSCOUNT_DURATION,
                "syscount_timeout": SYSCOUNT_TIMEOUT,
                "allow_missing_userbpf": ALLOW_MISSING_USERBPF,
                "trace_log_root": str(TRACE_LOG_ROOT),
            },
            "stats": run_stats,
            "trace_observability": trace_observability,
            "results": results
        }, f, indent=2)
    
    print(f"\nResults saved to {filename}")
    return filename

def generate_report(avgs, result_filename, timestamp):
    """Generate a Markdown report from benchmark results"""
    print("\n=== Generating Markdown Report ===")
    
    # Dictionary for formatted names for printing
    name_map = {
        "native": "Native (No tracing)",
        "kernel_targeted": "Kernel syscount (targeting nginx)",
        "kernel_untargeted": "Kernel syscount (not targeting nginx)",
        "userbpf_targeted": "UserBPF syscount (targeting nginx)",
        "userbpf_untargeted": "UserBPF syscount (not targeting nginx)"
    }
    
    # Calculate percentage comparisons
    comparisons = {}
    if "native" in avgs:
        native_avg = avgs["native"]
        for test_name, avg in avgs.items():
            if test_name != "native":
                impact = ((avg - native_avg) / native_avg) * 100
                comparisons[f"{test_name}_vs_native"] = impact
    
    # Compare userBPF to kernel
    if "kernel_targeted" in avgs and "userbpf_targeted" in avgs:
        kernel_avg = avgs["kernel_targeted"]
        userbpf_avg = avgs["userbpf_targeted"]
        improvement = ((userbpf_avg - kernel_avg) / kernel_avg) * 100
        comparisons["userbpf_vs_kernel_targeted"] = improvement
    
    if "kernel_untargeted" in avgs and "userbpf_untargeted" in avgs:
        kernel_avg = avgs["kernel_untargeted"]
        userbpf_avg = avgs["userbpf_untargeted"]
        improvement = ((userbpf_avg - kernel_avg) / kernel_avg) * 100
        comparisons["userbpf_vs_kernel_untargeted"] = improvement
    
    # Generate report content
    report = [
        "# Benchmark Report: syscount-nginx Performance Analysis",
        "",
        "## Overview",
        "This report analyzes the performance of nginx under different syscall counting methods, comparing native execution (no tracing), kernel-based syscount (targeting nginx and not targeting nginx), and bpftime's userspace BPF implementation (targeting nginx and not targeting nginx).",
        "",
        "## Test Environment",
        f"- **Test Date**: {timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}",
        f"- **Benchmark Tool**: wrk (`{TEST_URL}`, concurrency: 10, duration: 10s)",
        f"- **Number of Runs**: {NUM_RUNS}",
        "",
        "## Performance Results",
        "",
        "| Configuration | Requests/sec | % vs Native | % vs Kernel (same targeting) |",
        "|---------------|--------------|-------------|------------------------------|"
    ]
    
    # Add table rows
    if "native" in avgs:
        report.append(f"| {name_map['native']} | {avgs['native']:,.2f} | - | - |")
    
    if "kernel_targeted" in avgs:
        vs_native = comparisons.get("kernel_targeted_vs_native", 0)
        report.append(f"| {name_map['kernel_targeted']} | {avgs['kernel_targeted']:,.2f} | {vs_native:+.2f}% | - |")
    
    if "kernel_untargeted" in avgs:
        vs_native = comparisons.get("kernel_untargeted_vs_native", 0)
        report.append(f"| {name_map['kernel_untargeted']} | {avgs['kernel_untargeted']:,.2f} | {vs_native:+.2f}% | - |")
    
    if "userbpf_targeted" in avgs:
        vs_native = comparisons.get("userbpf_targeted_vs_native", 0)
        vs_kernel = comparisons.get("userbpf_vs_kernel_targeted", 0)
        report.append(f"| {name_map['userbpf_targeted']} | {avgs['userbpf_targeted']:,.2f} | {vs_native:+.2f}% | {vs_kernel:+.2f}% |")
    
    if "userbpf_untargeted" in avgs:
        vs_native = comparisons.get("userbpf_untargeted_vs_native", 0)
        vs_kernel = comparisons.get("userbpf_vs_kernel_untargeted", 0)
        report.append(f"| {name_map['userbpf_untargeted']} | {avgs['userbpf_untargeted']:,.2f} | {vs_native:+.2f}% | {vs_kernel:+.2f}% |")
    
    # Add key findings
    report.extend([
        "",
        "## Key Findings",
        "",
        "1. **Performance Comparison with Native Baseline**"
    ])
    
    if all(comp > 0 for comp in [
        comparisons.get("kernel_targeted_vs_native", 0),
        comparisons.get("kernel_untargeted_vs_native", 0),
        comparisons.get("userbpf_targeted_vs_native", 0),
        comparisons.get("userbpf_untargeted_vs_native", 0)
    ]):
        report.extend([
            "   - All tracing methods outperformed the native baseline.",
            "   - This unexpected result might be due to caching effects or statistical variation.",
            "   - In a more robust test with more runs, we would typically expect some performance penalty for tracing."
        ])
    else:
        report.append("   - There's a mixed performance impact when comparing with the native baseline.")
    
    report.extend([
        "",
        "2. **UserBPF vs Kernel-based syscount**"
    ])
    
    if "userbpf_vs_kernel_targeted" in comparisons:
        if comparisons["userbpf_vs_kernel_targeted"] > 0:
            report.append(f"   - When targeting nginx specifically, UserBPF showed {comparisons['userbpf_vs_kernel_targeted']:.2f}% better performance than the kernel equivalent.")
        else:
            report.append(f"   - When targeting nginx specifically, UserBPF showed {-comparisons['userbpf_vs_kernel_targeted']:.2f}% worse performance than the kernel equivalent.")
    
    if "userbpf_vs_kernel_untargeted" in comparisons:
        if comparisons["userbpf_vs_kernel_untargeted"] > 0:
            report.append(f"   - When not targeting nginx, UserBPF showed {comparisons['userbpf_vs_kernel_untargeted']:.2f}% better performance than the kernel equivalent.")
        else:
            report.append(f"   - When not targeting nginx, UserBPF showed {-comparisons['userbpf_vs_kernel_untargeted']:.2f}% worse performance than the kernel equivalent.")
    
    # Add targeted vs not-targeting-nginx comparison
    report.extend([
        "",
        "3. **Targeted vs. Untargeted Performance**"
    ])
    
    if "kernel_targeted" in avgs and "kernel_untargeted" in avgs:
        pct_diff = ((avgs["kernel_untargeted"] - avgs["kernel_targeted"]) / avgs["kernel_targeted"]) * 100
        if pct_diff > 0:
            report.append(f"   - For kernel-based tracing, the not-targeting-nginx mode performed {pct_diff:.2f}% better than targeted mode.")
        else:
            report.append(f"   - For kernel-based tracing, the targeted mode performed {-pct_diff:.2f}% better than not-targeting-nginx mode.")
    
    if "userbpf_targeted" in avgs and "userbpf_untargeted" in avgs:
        pct_diff = ((avgs["userbpf_untargeted"] - avgs["userbpf_targeted"]) / avgs["userbpf_targeted"]) * 100
        if pct_diff > 0:
            report.append(f"   - For UserBPF, the not-targeting-nginx mode performed {pct_diff:.2f}% better than targeted mode.")
        else:
            report.append(f"   - For UserBPF, the targeted mode performed {-pct_diff:.2f}% better than not-targeting-nginx mode.")
    
    # Add conclusion
    report.extend([
        "",
        "## Conclusion",
        "",
        "The benchmark results demonstrate that bpftime's userspace BPF implementation provides performance characteristics that differ from traditional kernel-based syscount for syscall tracing.",
        "",
        "This data suggests that userspace BPF may offer benefits for observability tools that need to monitor production systems with minimal overhead.",
        "",
        "## Recommendations",
        "",
        "1. **Extend testing with more runs**: Multiple benchmark runs with different loads would provide more statistical confidence.",
        "",
        "2. **Profile resource usage**: Adding CPU, memory, and I/O metrics would provide deeper insights into the efficiency differences.",
        "",
        "3. **Test with varied workloads**: Different nginx configurations and request patterns could reveal performance characteristics under various conditions.",
        "",
        f"## Raw Data",
        "",
        f"The raw benchmark data is available in the JSON file: `{result_filename}`",
        "",
        f"![Benchmark Results](benchmark_chart.png)"
    ])
    
    # Write report to file
    report_path = "benchmark/syscount-nginx/reports.md"
    with open(report_path, 'w') as f:
        f.write('\n'.join(report))
    
    print(f"Markdown report generated: {report_path}")
    return report_path

def generate_chart(avgs, timestamp):
    """Generate a bar chart visualization of benchmark results"""
    print("\n=== Generating Benchmark Chart ===")
    
    try:
        # Set the style
        plt.style.use('seaborn-v0_8-whitegrid')
        
        # Use a nicer font if available
        try:
            mpl.rcParams['font.family'] = 'DejaVu Sans'
        except:
            pass
        
        # Prepare data for plotting
        categories = [
            'Native\n(No tracing)',
            'Kernel\n(targeting nginx)',
            'Kernel\n(not targeting nginx)',
            'UserBPF\n(targeting nginx)',
            'UserBPF\n(not targeting nginx)'
        ]
        
        metrics = [
            avgs.get('native', 0),
            avgs.get('kernel_targeted', 0),
            avgs.get('kernel_untargeted', 0),
            avgs.get('userbpf_targeted', 0),
            avgs.get('userbpf_untargeted', 0)
        ]
        
        # Set up figure size
        plt.figure(figsize=(12, 8))
        
        # Set up colors
        colors = ['#4C72B0', '#DD8452', '#55A868', '#C44E52', '#8172B3']
        
        # Create bar chart
        bars = plt.bar(categories, metrics, color=colors, width=0.6)
        
        # Add values on top of bars
        for bar in bars:
            height = bar.get_height()
            if height > 0:  # Only add text if there's a value
                plt.text(bar.get_x() + bar.get_width()/2., height + 1000,
                        f'{height:,.2f}',
                        ha='center', va='bottom', fontsize=9)
        
        # Adjust layout
        plt.title('Nginx Performance Under Different Syscount Methods', fontsize=16, pad=20)
        plt.ylabel('Requests per Second', fontsize=14)
        plt.ylim(0, max(metrics) * 1.15 if max(metrics) > 0 else 100000)  # Add 15% space for labels
        
        # Add grid
        plt.grid(axis='y', linestyle='--', alpha=0.7)
        
        # Annotate with runs information
        plt.annotate(f'Number of benchmark runs: {NUM_RUNS}', 
                    xy=(0.02, 0.97), xycoords='figure fraction',
                    fontsize=10, ha='left', va='top',
                    bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8))
        
        # Add percentage comparisons
        if avgs.get('native', 0) > 0:  # If we have native results
            # Calculate percentages vs native
            for i in range(1, len(metrics)):
                if metrics[i] > 0:  # Only add comparison if there's a value
                    pct_diff = ((metrics[i] - metrics[0]) / metrics[0]) * 100
                    plt.annotate(f'{pct_diff:+.2f}% vs native', 
                                xy=(i, metrics[i] - metrics[i]*0.05), 
                                ha='center', va='top',
                                fontsize=9, color='black',
                                bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.7))
            
            # Add UserBPF vs Kernel comparisons
            k_targ, k_untarg, u_targ, u_untarg = metrics[1:5]
            
            if k_targ > 0 and u_targ > 0:
                pct_diff = ((u_targ - k_targ) / k_targ) * 100
                plt.annotate(f'{pct_diff:+.2f}% vs Kernel(targeted)', 
                            xy=(3, metrics[3] - metrics[3]*0.15), 
                            ha='center', va='top',
                            fontsize=9, color='darkgreen',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.7))
            
            if k_untarg > 0 and u_untarg > 0:
                pct_diff = ((u_untarg - k_untarg) / k_untarg) * 100
                plt.annotate(f'{pct_diff:+.2f}% vs Kernel(not targeting nginx)', 
                            xy=(4, metrics[4] - metrics[4]*0.15), 
                            ha='center', va='top',
                            fontsize=9, color='darkgreen',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.7))
        
        # Add watermark
        plt.figtext(0.5, 0.02, f'bpftime syscount-nginx benchmark - {timestamp}', 
                   ha='center', va='bottom', fontsize=10, style='italic', alpha=0.7)
        
        # Tight layout
        plt.tight_layout()
        
        # Save figure
        chart_path = "benchmark/syscount-nginx/benchmark_chart.png"
        plt.savefig(chart_path, dpi=300, bbox_inches='tight')
        print(f"Chart saved to: {chart_path}")
        
        # Try to display (won't work in headless environment)
        try:
            plt.show()
        except:
            pass
            
        return chart_path
        
    except Exception as e:
        debug_print(f"Error generating chart: {e}")
        traceback.print_exc()
        return None

def main():
    try:
        # For testing, reduce the number of runs
        global NUM_RUNS
        if "--test" in sys.argv:
            NUM_RUNS = 1
            debug_print("Running in test mode with NUM_RUNS=1")
        
        debug_print("Starting benchmark script")
        debug_print(f"Current working directory: {os.getcwd()}")
        
        # Check if nginx is installed
        nginx_installed = check_command_exists(NGINX_BIN)
        if not nginx_installed:
            debug_print(f"ERROR: nginx command not found: {NGINX_BIN}")
            debug_print("Please install nginx before running this benchmark or set SYSCOUNT_NGINX_BIN.")
            debug_print("On Ubuntu/Debian: sudo apt-get install nginx")
            debug_print("On CentOS/RHEL: sudo yum install nginx")
            return
        
        # Check if we're in the right directory
        if not os.path.exists("benchmark/syscount-nginx/nginx.conf"):
            debug_print("nginx.conf not found in expected location")
            debug_print("Checking if we need to adjust paths...")
            
            # Try to find the correct directory
            script_dir = os.path.dirname(os.path.abspath(__file__))
            debug_print(f"Script directory: {script_dir}")
            
            # If we're running from the benchmark/syscount-nginx directory, adjust paths
            if os.path.basename(script_dir) == "syscount-nginx":
                debug_print("Running from syscount-nginx directory, adjusting paths")
                os.chdir(os.path.dirname(os.path.dirname(script_dir)))  # Go up two levels
                debug_print(f"New working directory: {os.getcwd()}")
        
        # Check if we can access necessary files
        for path in [AGENT_PATH, SYSCALL_SERVER_PATH, SYSCOUNT_PATH]:
            check_file_exists(path)
        
        # Check if nginx.conf exists
        nginx_conf_path = "benchmark/syscount-nginx/nginx.conf"
        if not os.path.exists(nginx_conf_path):
            debug_print(f"ERROR: {nginx_conf_path} not found!")
            debug_print("This is required for the benchmark to run.")
            return
        
        # Check if wrk is available
        if not check_command_exists("wrk"):
            debug_print("ERROR: wrk command not found!")
            debug_print("Please install wrk before running this benchmark.")
            return
        
        # Check for matplotlib
        try:
            import matplotlib
            debug_print(f"Matplotlib version: {matplotlib.__version__}")
        except ImportError:
            debug_print("WARNING: Matplotlib not found. Will skip chart generation.")
            debug_print("Install matplotlib with: pip install matplotlib")
        
        # Run benchmarks
        run_native()                      # No tracing
        run_kernel_syscount(target_pid=True)  # Kernel syscount targeting nginx
        run_kernel_syscount(target_pid=False) # Kernel syscount not targeting nginx
        run_userbpf_syscount(target_pid=True) # UserBPF syscount targeting nginx
        run_userbpf_syscount(target_pid=False)# UserBPF syscount not targeting nginx
        
        # Print statistics and get averages
        avgs = print_statistics()
        
        # Save results to JSON
        result_filename = save_results()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Generate report and chart
        report_path = generate_report(avgs, os.path.basename(result_filename), timestamp)
        chart_path = generate_chart(avgs, timestamp)
        
        if report_path and chart_path:
            print("\n=== Benchmark Complete ===")
            print(f"Report: {report_path}")
            print(f"Chart: {chart_path}")

        missing_results = [
            name for name, values in results.items()
            if (
                not values
                and not run_stats[name].get("skipped")
                and not (ALLOW_MISSING_USERBPF and name.startswith("userbpf_"))
            )
        ]
        if missing_results:
            debug_print(f"ERROR: missing valid results for: {', '.join(missing_results)}")
            sys.exit(1)
        
    except KeyboardInterrupt:
        print("\nBenchmark interrupted.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        traceback.print_exc()
    finally:
        debug_print("Cleaning up before exit")
        cleanup_processes()

if __name__ == "__main__":
    main()
