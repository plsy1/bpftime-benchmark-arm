#!/usr/bin/env python3
import subprocess
import re
import os
import time
import json
import shlex
import statistics
import signal
import sys
import traceback
from datetime import datetime

# Configuration
NUM_RUNS = int(os.environ.get("SSL_NGINX_NUM_RUNS", "10"))
WRK_CONNECTIONS = os.environ.get("SSL_NGINX_WRK_CONNECTIONS", "100")
WRK_DURATION = os.environ.get("SSL_NGINX_WRK_DURATION", "10")
WRK_TIMEOUT = int(os.environ.get("SSL_NGINX_WRK_TIMEOUT", "15"))
BPFTIME_RETRIES = int(os.environ.get("SSL_NGINX_BPFTIME_RETRIES", "0"))
READY_TIMEOUT = int(os.environ.get("SSL_NGINX_READY_TIMEOUT", "10"))
TRACE_CHECK_TIMEOUT = int(os.environ.get("SSL_NGINX_TRACE_CHECK_TIMEOUT", "5"))
WRK_CMD = ["wrk", "https://127.0.0.1:4043/index.html", "-c", WRK_CONNECTIONS, "-d", WRK_DURATION]
NGINX_CMD = ["nginx", "-c", "nginx.conf", "-p", "benchmark/ssl-nginx"]
TEST_URL = "https://127.0.0.1:4043/index.html"
SSLSNIFF_PATH = "example/tracing/sslsniff/sslsniff"
KERNEL_SSLSNIFF_PATH = "example/tracing/sslsniff/sslsniff"
SSLSNIFF_ARGS = shlex.split(os.environ.get("SSL_NGINX_SSLSNIFF_ARGS", ""))
AGENT_PATH = "build/runtime/agent/libbpftime-agent.so"
SYSCALL_SERVER_PATH = "build/runtime/syscall-server/libbpftime-syscall-server.so"
BENCH_ORDER = [
    item.strip()
    for item in os.environ.get("SSL_NGINX_BENCH_ORDER", "baseline,kernel,bpftime").split(",")
    if item.strip()
]
BENCH_RESULT_KEYS = {
    "baseline": "baseline",
    "kernel": "kernel_sslsniff",
    "kernel_sslsniff": "kernel_sslsniff",
    "bpftime": "bpftime_sslsniff",
    "bpftime_sslsniff": "bpftime_sslsniff",
}
ACTIVE_RESULT_KEYS = {
    BENCH_RESULT_KEYS[item]
    for item in BENCH_ORDER
    if item in BENCH_RESULT_KEYS
}

# Result storage
results = {
    "baseline": [],
    "kernel_sslsniff": [],
    "bpftime_sslsniff": []
}
run_stats = {
    "baseline": {
        "target_runs": NUM_RUNS,
        "valid_runs": 0,
        "failed_attempts": 0,
        "timeouts": 0,
        "parse_failures": 0,
        "wrk_failures": 0,
        "exceptions": 0,
    },
    "kernel_sslsniff": {
        "target_runs": NUM_RUNS,
        "valid_runs": 0,
        "failed_attempts": 0,
        "timeouts": 0,
        "parse_failures": 0,
        "wrk_failures": 0,
        "attach_failures": 0,
        "exceptions": 0,
    },
    "bpftime_sslsniff": {
        "target_runs": NUM_RUNS,
        "valid_runs": 0,
        "failed_attempts": 0,
        "timeouts": 0,
        "parse_failures": 0,
        "wrk_failures": 0,
        "readiness_failures": 0,
        "attach_failures": 0,
        "retry_attempts": 0,
        "exceptions": 0,
    },
}
trace_observability = {
    "kernel_sslsniff": [],
    "bpftime_sslsniff": [],
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
    print(f"[DEBUG {timestamp}] {message}", flush=True)

def mark_valid(test_name):
    run_stats[test_name]["valid_runs"] += 1

def mark_failed(test_name, reason):
    run_stats[test_name]["failed_attempts"] += 1
    if reason in run_stats[test_name]:
        run_stats[test_name][reason] += 1

def summarize_trace_output(test_name, stdout_text, stderr_text, returncode):
    event_count = sum(
        1 for line in stdout_text.splitlines()
        if line.startswith(("READ/RECV", "WRITE/SEND", "HANDSHAKE"))
    )
    attach_markers = [
        marker for marker in ("OpenSSL path:", "GnuTLS path:", "NSS path:")
        if marker in stdout_text
    ]
    hard_error_markers = [
        marker for marker in (
            "no program attached",
            "failed to open perf buffer",
            "ERROR:",
            "Error:",
        )
        if marker in stdout_text or marker in stderr_text
    ]
    summary = {
        "returncode": returncode,
        "event_count": event_count,
        "attach_markers": attach_markers,
        "attach_started": bool(attach_markers),
        "attach_success": bool(attach_markers) and not hard_error_markers,
        "error_markers": hard_error_markers,
        "stdout_bytes": len(stdout_text.encode(errors="replace")),
        "stderr_bytes": len(stderr_text.encode(errors="replace")),
    }
    trace_observability[test_name].append(summary)
    debug_print(
        f"{test_name} trace preflight: attach_success={summary['attach_success']} "
        f"events={summary['event_count']} returncode={returncode}"
    )
    return summary

def terminate_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

def collect_trace_preflight(test_name, cmd, env=None):
    debug_print(f"Starting {test_name} trace preflight: {' '.join(cmd)}")
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        time.sleep(2)
        if proc.poll() is not None:
            stdout_text, stderr_text = proc.communicate(timeout=1)
            return summarize_trace_output(test_name, stdout_text or "", stderr_text or "", proc.returncode)

        subprocess.run(["curl", "-k", "-s", "-o", "/dev/null", TEST_URL], timeout=TRACE_CHECK_TIMEOUT)
        terminate_process(proc)
        stdout_text, stderr_text = proc.communicate(timeout=TRACE_CHECK_TIMEOUT)
        return summarize_trace_output(test_name, stdout_text or "", stderr_text or "", proc.returncode)
    except Exception as e:
        debug_print(f"{test_name} trace preflight failed: {e}")
        terminate_process(proc)
        trace_observability[test_name].append({
            "returncode": proc.poll() if proc else None,
            "event_count": 0,
            "attach_markers": [],
            "attach_started": False,
            "attach_success": False,
            "error_markers": [str(e)],
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        })
        return trace_observability[test_name][-1]

def trace_is_effective(summary):
    return summary["attach_success"] and summary["event_count"] > 0

def mark_trace_preflight_failed(test_name, summary):
    mark_failed(test_name, "attach_failures")
    debug_print(
        f"{test_name} trace preflight did not observe a valid trace: "
        f"attach_success={summary.get('attach_success')} events={summary.get('event_count')} "
        f"errors={summary.get('error_markers')}"
    )

def remove_access_log():
    """Remove the nginx access log file"""
    # The log file is typically in the logs directory under the nginx directory
    log_path = os.path.join("benchmark", "ssl-nginx", "access.log")
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

def check_file_exists(path):
    """Check if a file exists and print its absolute path"""
    abs_path = os.path.abspath(path)
    exists = os.path.exists(abs_path)
    debug_print(f"Checking file: {abs_path} - {'EXISTS' if exists else 'NOT FOUND'}")
    return exists

def check_command_exists(cmd):
    """Check if a command exists in PATH"""
    try:
        result = subprocess.run(["which", cmd], capture_output=True, text=True)
        exists = result.returncode == 0
        debug_print(f"Checking command: {cmd} - {'EXISTS' if exists else 'NOT FOUND'}")
        
        if exists:
            debug_print(f"  Path: {result.stdout.strip()}")
        else:
            # For nginx, check common locations
            if cmd == "nginx":
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
    """Kill any running nginx or sslsniff processes"""
    try:
        debug_print("Cleaning up processes...")
        
        # Get the PIDs of nginx and sslsniff processes
        nginx_pids = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, text=True).stdout.strip().split()
        sslsniff_pids = subprocess.run(["pgrep", "-x", "sslsniff"], capture_output=True, text=True).stdout.strip().split()
        
        debug_print(f"Found nginx PIDs: {nginx_pids}")
        debug_print(f"Found sslsniff PIDs: {sslsniff_pids}")
        
        # Kill nginx processes by PID
        for pid in nginx_pids:
            try:
                debug_print(f"Terminating nginx process with PID {pid}")
                subprocess.run(["kill", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error terminating nginx PID {pid}: {e}")
        
        # Kill sslsniff processes by PID
        for pid in sslsniff_pids:
            try:
                debug_print(f"Terminating sslsniff process with PID {pid}")
                subprocess.run(["kill", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error terminating sslsniff PID {pid}: {e}")
                try:
                    debug_print(f"Trying with sudo...")
                    subprocess.run(["sudo", "kill", pid], stderr=subprocess.DEVNULL, check=False)
                except Exception as e2:
                    debug_print(f"Error with sudo: {e2}")
        
        # Wait for processes to terminate
        time.sleep(1)
        
        # Check if any processes are still running and try forceful termination if needed
        remaining_nginx_pids = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, text=True).stdout.strip().split()
        remaining_sslsniff_pids = subprocess.run(["pgrep", "-x", "sslsniff"], capture_output=True, text=True).stdout.strip().split()
        
        debug_print(f"After cleanup: nginx PIDs: {remaining_nginx_pids}, sslsniff PIDs: {remaining_sslsniff_pids}")
        
        # Force kill remaining processes
        for pid in remaining_nginx_pids:
            try:
                debug_print(f"Force killing nginx process with PID {pid}")
                subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error force killing nginx PID {pid}: {e}")
        
        for pid in remaining_sslsniff_pids:
            try:
                debug_print(f"Force killing sslsniff process with PID {pid}")
                subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL, check=False)
            except Exception as e:
                debug_print(f"Error force killing sslsniff PID {pid}: {e}")
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

def wait_for_nginx_ready(proc, label):
    """Wait until nginx serves the test URL, or report why it did not."""
    deadline = time.time() + READY_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            debug_print(f"{label} exited before readiness check. Exit code: {proc.returncode}")
            debug_print(f"{label} stdout: {stdout.decode(errors='replace') if stdout else 'None'}")
            debug_print(f"{label} stderr: {stderr.decode(errors='replace') if stderr else 'None'}")
            return False
        try:
            curl_check = subprocess.run(
                ["curl", "-k", "-s", "-o", "/dev/null", "-w", "%{http_code}", TEST_URL],
                capture_output=True,
                text=True,
                timeout=3,
            )
            last_status = curl_check.stdout.strip()
            if last_status == "200":
                debug_print(f"{label} readiness check passed with HTTP 200")
                return True
        except Exception as e:
            last_status = str(e)
        time.sleep(0.5)
    debug_print(f"{label} readiness check failed. Last status/error: {last_status}")
    return False

def run_baseline():
    """Run baseline benchmarks (no sslsniff)"""
    print("\n=== Running Baseline Tests ===")
    cleanup_processes()
    remove_access_log()  # Remove access log before starting nginx
    
    # Check if nginx exists
    check_command_exists("nginx")
    
    # Check current directory
    debug_print(f"Current directory: {os.getcwd()}")
    nginx_conf = "benchmark/ssl-nginx/nginx.conf"
    debug_print(f"Checking if nginx.conf exists: {os.path.exists(nginx_conf)}")
    
    if not os.path.exists(nginx_conf):
        debug_print(f"ERROR: {nginx_conf} not found!")
        return
    
    # Start nginx with full path
    abs_nginx_conf = os.path.abspath(nginx_conf)
    abs_nginx_dir = os.path.dirname(abs_nginx_conf)
    modified_nginx_cmd = ["nginx", "-c", abs_nginx_conf, "-p", abs_nginx_dir]
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
            return
        
        debug_print("nginx started successfully")
        
        # Check if nginx is listening
        try:
            curl_check = subprocess.run(["curl", "-k", "-s", "-o", "/dev/null", "-w", "%{http_code}", TEST_URL], 
                                        capture_output=True, text=True, timeout=5)
            debug_print(f"HTTP status code from nginx: {curl_check.stdout}")
        except Exception as e:
            debug_print(f"Error checking nginx: {e}")
        
        for i in range(NUM_RUNS):
            print(f"Run {i+1}/{NUM_RUNS}...")
            debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
            
            # Run wrk
            try:
                result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
                debug_print(f"wrk exit code: {result.returncode}")
                
                if result.returncode != 0:
                    mark_failed("baseline", "wrk_failures")
                    debug_print(f"wrk failed with exit code {result.returncode}")
                    debug_print(f"stdout: {result.stdout}")
                    debug_print(f"stderr: {result.stderr}")
                    continue
                
                req_per_sec = parse_wrk_output(result.stdout)
                if req_per_sec:
                    results["baseline"].append(req_per_sec)
                    mark_valid("baseline")
                    print(f"  Requests/sec: {req_per_sec:.2f}")
                else:
                    mark_failed("baseline", "parse_failures")
                    debug_print(f"Failed to parse output: {result.stdout}")
            except subprocess.TimeoutExpired:
                mark_failed("baseline", "timeouts")
                debug_print("wrk command timed out")
            except Exception as e:
                mark_failed("baseline", "exceptions")
                debug_print(f"Error running wrk: {e}")
                traceback.print_exc()
    
    except Exception as e:
        debug_print(f"Error in baseline test: {e}")
        traceback.print_exc()
    finally:
        # Cleanup
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

def run_kernel_sslsniff():
    """Run kernel sslsniff benchmarks"""
    print("\n=== Running Kernel sslsniff Tests ===")
    cleanup_processes()
    remove_access_log()  # Remove access log before starting nginx
    
    # Check if sslsniff exists
    if not check_file_exists(KERNEL_SSLSNIFF_PATH):
        debug_print(f"Skipping kernel sslsniff tests: {KERNEL_SSLSNIFF_PATH} not found")
        return
    
    # Use the same nginx path approach as baseline
    nginx_conf = "benchmark/ssl-nginx/nginx.conf"
    if not os.path.exists(nginx_conf):
        debug_print(f"ERROR: {nginx_conf} not found!")
        return
    
    # Start nginx with full path
    abs_nginx_conf = os.path.abspath(nginx_conf)
    abs_nginx_dir = os.path.dirname(abs_nginx_conf)
    modified_nginx_cmd = ["nginx", "-c", abs_nginx_conf, "-p", abs_nginx_dir]
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
            return

        kernel_cmd = ["sudo", KERNEL_SSLSNIFF_PATH, *SSLSNIFF_ARGS]
        trace_summary = collect_trace_preflight("kernel_sslsniff", kernel_cmd)
        subprocess.run(["sudo", "pkill", "-f", "sslsniff"], stderr=subprocess.DEVNULL)
        if not trace_is_effective(trace_summary):
            mark_trace_preflight_failed("kernel_sslsniff", trace_summary)
            return
        
        for i in range(NUM_RUNS):
            print(f"Run {i+1}/{NUM_RUNS}...")
            
            # Start sslsniff
            debug_print(f"Starting kernel sslsniff: {' '.join(kernel_cmd)}")
            sslsniff_proc = None
            try:
                sslsniff_proc = subprocess.Popen(kernel_cmd,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                time.sleep(2)  # Give sslsniff time to start
                if sslsniff_proc.poll() is not None:
                    mark_failed("kernel_sslsniff", "attach_failures")
                    debug_print(f"kernel sslsniff exited before wrk. Exit code: {sslsniff_proc.returncode}")
                    continue
                
                # Run wrk
                debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
                result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
                debug_print(f"wrk exit code: {result.returncode}")
                if result.stderr:
                    debug_print(f"wrk stderr: {result.stderr}")
                if result.returncode != 0:
                    mark_failed("kernel_sslsniff", "wrk_failures")
                    debug_print(f"wrk failed stdout: {result.stdout}")
                    continue
                req_per_sec = parse_wrk_output(result.stdout)

                if req_per_sec:
                    results["kernel_sslsniff"].append(req_per_sec)
                    mark_valid("kernel_sslsniff")
                    print(f"  Requests/sec: {req_per_sec:.2f}")
                else:
                    mark_failed("kernel_sslsniff", "parse_failures")
                    debug_print(f"Failed to parse output: {result.stdout}")
            except subprocess.TimeoutExpired:
                mark_failed("kernel_sslsniff", "timeouts")
                debug_print("wrk command timed out")
            except Exception as e:
                mark_failed("kernel_sslsniff", "exceptions")
                debug_print(f"Error in kernel sslsniff run: {e}")
                traceback.print_exc()
            finally:
                debug_print("Killing sslsniff")
                terminate_process(sslsniff_proc)
                subprocess.run(["sudo", "pkill", "-f", "sslsniff"], stderr=subprocess.DEVNULL)
                time.sleep(1)
    
    except Exception as e:
        debug_print(f"Error in kernel sslsniff test: {e}")
        traceback.print_exc()
    finally:
        # Cleanup
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

def run_bpftime_sslsniff():
    """Run bpftime sslsniff benchmarks"""
    print("\n=== Running bpftime sslsniff Tests ===")
    cleanup_processes()
    remove_access_log()  # Remove access log before starting nginx
    
    # Check if required files exist
    if not check_file_exists(SSLSNIFF_PATH) or not check_file_exists(AGENT_PATH) or not check_file_exists(SYSCALL_SERVER_PATH):
        debug_print("Skipping bpftime sslsniff tests: required files not found")
        return
    
    # Use the same nginx path approach as baseline
    nginx_conf = "benchmark/ssl-nginx/nginx.conf"
    if not os.path.exists(nginx_conf):
        debug_print(f"ERROR: {nginx_conf} not found!")
        return
    
    # Start nginx with full path
    abs_nginx_conf = os.path.abspath(nginx_conf)
    abs_nginx_dir = os.path.dirname(abs_nginx_conf)
    modified_nginx_cmd = ["nginx", "-c", abs_nginx_conf, "-p", abs_nginx_dir]

    preflight_nginx_proc = None
    preflight_sslsniff_proc = None
    try:
        bpftime_cmd = [SSLSNIFF_PATH, *SSLSNIFF_ARGS]
        sslsniff_env = os.environ.copy()
        sslsniff_env["LD_PRELOAD"] = SYSCALL_SERVER_PATH
        debug_print(f"Starting bpftime sslsniff trace preflight: LD_PRELOAD={SYSCALL_SERVER_PATH} {' '.join(bpftime_cmd)}")
        preflight_sslsniff_proc = subprocess.Popen(
            bpftime_cmd,
            env=sslsniff_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        time.sleep(2)
        if preflight_sslsniff_proc.poll() is not None:
            stdout_text, stderr_text = preflight_sslsniff_proc.communicate(timeout=1)
            trace_summary = summarize_trace_output(
                "bpftime_sslsniff",
                stdout_text or "",
                stderr_text or "",
                preflight_sslsniff_proc.returncode,
            )
            mark_trace_preflight_failed("bpftime_sslsniff", trace_summary)
            return

        preflight_nginx_env = os.environ.copy()
        preflight_nginx_env["LD_PRELOAD"] = AGENT_PATH
        preflight_nginx_proc = subprocess.Popen(
            modified_nginx_cmd,
            env=preflight_nginx_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)
        if not wait_for_nginx_ready(preflight_nginx_proc, "bpftime nginx preflight"):
            mark_failed("bpftime_sslsniff", "readiness_failures")
            return
        subprocess.run(["curl", "-k", "-s", "-o", "/dev/null", TEST_URL], timeout=TRACE_CHECK_TIMEOUT)
        terminate_process(preflight_sslsniff_proc)
        stdout_text, stderr_text = preflight_sslsniff_proc.communicate(timeout=TRACE_CHECK_TIMEOUT)
        trace_summary = summarize_trace_output(
            "bpftime_sslsniff",
            stdout_text or "",
            stderr_text or "",
            preflight_sslsniff_proc.returncode,
        )
        if not trace_is_effective(trace_summary):
            mark_trace_preflight_failed("bpftime_sslsniff", trace_summary)
            return
    finally:
        terminate_process(preflight_nginx_proc)
        terminate_process(preflight_sslsniff_proc)
        cleanup_processes()
    
    for i in range(NUM_RUNS):
        print(f"Run {i+1}/{NUM_RUNS}...")

        for attempt in range(BPFTIME_RETRIES + 1):
            if attempt:
                run_stats["bpftime_sslsniff"]["retry_attempts"] += 1
                debug_print(f"Retrying bpftime run {i+1}/{NUM_RUNS}, attempt {attempt+1}/{BPFTIME_RETRIES+1}")

            nginx_proc = None
            sslsniff_proc = None
            try:
                # Start sslsniff with bpftime
                bpftime_cmd = [SSLSNIFF_PATH, *SSLSNIFF_ARGS]
                debug_print(f"Starting sslsniff with bpftime: LD_PRELOAD={SYSCALL_SERVER_PATH} {' '.join(bpftime_cmd)}")
                env = os.environ.copy()
                env["LD_PRELOAD"] = SYSCALL_SERVER_PATH
                sslsniff_proc = subprocess.Popen(bpftime_cmd,
                                                env=env,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                time.sleep(2)  # Give sslsniff time to start
                if sslsniff_proc.poll() is not None:
                    mark_failed("bpftime_sslsniff", "attach_failures")
                    debug_print(f"bpftime sslsniff exited before nginx/wrk. Exit code: {sslsniff_proc.returncode}")
                    continue

                # Start nginx with bpftime
                debug_print(f"Starting nginx with bpftime: LD_PRELOAD={AGENT_PATH} {' '.join(modified_nginx_cmd)}")
                env = os.environ.copy()
                env["LD_PRELOAD"] = AGENT_PATH
                nginx_proc = subprocess.Popen(modified_nginx_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(2)  # Give nginx time to start

                if not wait_for_nginx_ready(nginx_proc, "bpftime nginx"):
                    mark_failed("bpftime_sslsniff", "readiness_failures")
                    continue

                # Run wrk
                debug_print(f"Running wrk with command: {' '.join(WRK_CMD)}")
                result = subprocess.run(WRK_CMD, capture_output=True, text=True, timeout=WRK_TIMEOUT)
                debug_print(f"wrk exit code: {result.returncode}")
                if result.stderr:
                    debug_print(f"wrk stderr: {result.stderr}")
                if result.returncode != 0:
                    mark_failed("bpftime_sslsniff", "wrk_failures")
                    debug_print(f"wrk failed stdout: {result.stdout}")
                    continue
                req_per_sec = parse_wrk_output(result.stdout)

                if req_per_sec:
                    results["bpftime_sslsniff"].append(req_per_sec)
                    mark_valid("bpftime_sslsniff")
                    print(f"  Requests/sec: {req_per_sec:.2f}")
                    break
                else:
                    mark_failed("bpftime_sslsniff", "parse_failures")
                    debug_print(f"Failed to parse stdout: {result.stdout}")
            except subprocess.TimeoutExpired:
                mark_failed("bpftime_sslsniff", "timeouts")
                debug_print("wrk command timed out")
            except Exception as e:
                mark_failed("bpftime_sslsniff", "exceptions")
                debug_print(f"Error in bpftime sslsniff run: {e}")
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
                terminate_process(sslsniff_proc)

                cleanup_processes()
        else:
            debug_print(f"bpftime run {i+1}/{NUM_RUNS} produced no valid result after {BPFTIME_RETRIES+1} attempts")

    debug_print(f"bpftime retry attempts: {run_stats['bpftime_sslsniff']['retry_attempts']}")
    debug_print(f"bpftime failed attempts: {run_stats['bpftime_sslsniff']['failed_attempts']}")

def print_statistics():
    """Print benchmark statistics"""
    print("\n=== Benchmark Results ===")
    print(f"Each configuration run {NUM_RUNS} times")
    failure_reasons = [
        "timeouts",
        "parse_failures",
        "wrk_failures",
        "readiness_failures",
        "attach_failures",
        "exceptions",
    ]
    
    for test_name, values in results.items():
        stats = run_stats[test_name]
        print(f"\n{test_name.replace('_', ' ').title()}:")
        print(f"  Valid runs:            {stats['valid_runs']}/{stats['target_runs']}")
        print(f"  Failed attempts:       {stats['failed_attempts']}")
        breakdown = [
            f"{reason}={stats[reason]}"
            for reason in failure_reasons
            if stats.get(reason, 0)
        ]
        if breakdown:
            print(f"  Failure breakdown:     {', '.join(breakdown)}")
        if stats.get("retry_attempts", 0):
            print(f"  Retry attempts:        {stats['retry_attempts']}")
        if not values:
            print("  No valid results")
            continue
            
        avg = statistics.mean(values)
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
    if results["baseline"] and results["kernel_sslsniff"]:
        baseline_avg = statistics.mean(results["baseline"])
        kernel_avg = statistics.mean(results["kernel_sslsniff"])
        kernel_impact = ((baseline_avg - kernel_avg) / baseline_avg) * 100
        print(f"\nKernel sslsniff performance impact: {kernel_impact:.2f}% decrease")
        
    if results["baseline"] and results["bpftime_sslsniff"]:
        baseline_avg = statistics.mean(results["baseline"])
        bpftime_avg = statistics.mean(results["bpftime_sslsniff"])
        bpftime_impact = ((baseline_avg - bpftime_avg) / baseline_avg) * 100
        print(f"bpftime sslsniff performance impact: {bpftime_impact:.2f}% decrease")
    
    if results["kernel_sslsniff"] and results["bpftime_sslsniff"]:
        kernel_avg = statistics.mean(results["kernel_sslsniff"])
        bpftime_avg = statistics.mean(results["bpftime_sslsniff"])
        improvement = ((bpftime_avg - kernel_avg) / kernel_avg) * 100
        print(f"bpftime improvement over kernel: {improvement:.2f}%")

    print("\nTrace observability:")
    for test_name, summaries in trace_observability.items():
        if not summaries:
            print(f"  {test_name}: no trace preflight")
            continue
        total_events = sum(item["event_count"] for item in summaries)
        attach_ok = sum(1 for item in summaries if item["attach_success"])
        print(
            f"  {test_name}: attach_success={attach_ok}/{len(summaries)} "
            f"events={total_events}"
        )

def save_results():
    """Save results to a JSON file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"benchmark/ssl-nginx/benchmark_results_{timestamp}.json"
    
    with open(filename, 'w') as f:
        json.dump({
            "timestamp": timestamp,
            "runs": NUM_RUNS,
            "config": {
                "wrk_cmd": WRK_CMD,
                "wrk_timeout": WRK_TIMEOUT,
                "ready_timeout": READY_TIMEOUT,
                "bpftime_retries": BPFTIME_RETRIES,
                "trace_check_timeout": TRACE_CHECK_TIMEOUT,
                "sslsniff_args": SSLSNIFF_ARGS,
                "bench_order": BENCH_ORDER,
            },
            "stats": run_stats,
            "trace_observability": trace_observability,
            "results": results
        }, f, indent=2)
    
    print(f"\nResults saved to {filename}")

def benchmark_failed():
    return any(
        stats["valid_runs"] < stats["target_runs"]
        for test_name, stats in run_stats.items()
        if test_name in ACTIVE_RESULT_KEYS
    )

def main():
    try:
        # For testing, reduce the number of runs
        global NUM_RUNS
        if "--test" in sys.argv:
            NUM_RUNS = 1
            debug_print("Running in test mode with NUM_RUNS=1")
        for stats in run_stats.values():
            stats["target_runs"] = NUM_RUNS
        
        debug_print("Starting benchmark script")
        debug_print(f"Current working directory: {os.getcwd()}")
        
        # Check if nginx is installed
        nginx_installed = check_command_exists("nginx")
        if not nginx_installed:
            debug_print("ERROR: nginx command not found!")
            debug_print("Please install nginx before running this benchmark.")
            debug_print("On Ubuntu/Debian: sudo apt-get install nginx")
            debug_print("On CentOS/RHEL: sudo yum install nginx")
            return
        
        # Check if we're in the right directory
        if not os.path.exists("benchmark/ssl-nginx/nginx.conf"):
            debug_print("nginx.conf not found in expected location")
            debug_print("Checking if we need to adjust paths...")
            
            # Try to find the correct directory
            script_dir = os.path.dirname(os.path.abspath(__file__))
            debug_print(f"Script directory: {script_dir}")
            
            # If we're running from the benchmark/ssl-nginx directory, adjust paths
            if os.path.basename(script_dir) == "ssl-nginx":
                debug_print("Running from ssl-nginx directory, adjusting paths")
                os.chdir(os.path.dirname(os.path.dirname(script_dir)))  # Go up two levels
                debug_print(f"New working directory: {os.getcwd()}")
        
        # Check if we can access necessary files
        for path in [AGENT_PATH, SYSCALL_SERVER_PATH, SSLSNIFF_PATH]:
            check_file_exists(path)
        
        # Check if nginx.conf exists
        nginx_conf_path = "benchmark/ssl-nginx/nginx.conf"
        if not os.path.exists(nginx_conf_path):
            debug_print(f"ERROR: {nginx_conf_path} not found!")
            debug_print("This is required for the benchmark to run.")
            return
        
        # Check if wrk is available
        if not check_command_exists("wrk"):
            debug_print("ERROR: wrk command not found!")
            debug_print("Please install wrk before running this benchmark.")
            return
        
        # Run benchmarks
        bench_runners = {
            "baseline": run_baseline,
            "kernel": run_kernel_sslsniff,
            "kernel_sslsniff": run_kernel_sslsniff,
            "bpftime": run_bpftime_sslsniff,
            "bpftime_sslsniff": run_bpftime_sslsniff,
        }
        for bench_name in BENCH_ORDER:
            runner = bench_runners.get(bench_name)
            if runner is None:
                debug_print(f"Unknown benchmark phase in SSL_NGINX_BENCH_ORDER: {bench_name}")
                sys.exit(1)
            runner()
        
        # Print and save results
        print_statistics()
        save_results()
        if benchmark_failed():
            debug_print("One or more benchmark phases did not produce the requested valid runs")
            sys.exit(1)
        
    except KeyboardInterrupt:
        print("\nBenchmark interrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        traceback.print_exc()
    finally:
        debug_print("Cleaning up before exit")
        cleanup_processes()

if __name__ == "__main__":
    main() 
