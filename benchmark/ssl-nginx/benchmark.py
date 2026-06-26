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

# Configuration
NUM_RUNS = int(os.environ.get("SSL_NGINX_NUM_RUNS", "10"))
WRK_CONNECTIONS = os.environ.get("SSL_NGINX_WRK_CONNECTIONS", "100")
WRK_DURATION = os.environ.get("SSL_NGINX_WRK_DURATION", "10")
WRK_TIMEOUT = int(os.environ.get("SSL_NGINX_WRK_TIMEOUT", "15"))
BPFTIME_RETRIES = int(os.environ.get("SSL_NGINX_BPFTIME_RETRIES", "0"))
READY_TIMEOUT = int(os.environ.get("SSL_NGINX_READY_TIMEOUT", "10"))
WRK_CMD = ["wrk", "https://127.0.0.1:4043/index.html", "-c", WRK_CONNECTIONS, "-d", WRK_DURATION]
NGINX_CMD = ["nginx", "-c", "nginx.conf", "-p", "benchmark/ssl-nginx"]
TEST_URL = "https://127.0.0.1:4043/index.html"
SSLSNIFF_PATH = "example/tracing/sslsniff/sslsniff"
KERNEL_SSLSNIFF_PATH = "example/tracing/sslsniff/sslsniff"
AGENT_PATH = "build/runtime/agent/libbpftime-agent.so"
SYSCALL_SERVER_PATH = "build/runtime/syscall-server/libbpftime-syscall-server.so"

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
        "retry_attempts": 0,
        "exceptions": 0,
    },
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
        
        for i in range(NUM_RUNS):
            print(f"Run {i+1}/{NUM_RUNS}...")
            
            # Start sslsniff
            debug_print(f"Starting kernel sslsniff: sudo {KERNEL_SSLSNIFF_PATH}")
            sslsniff_proc = None
            try:
                sslsniff_proc = subprocess.Popen(["sudo", KERNEL_SSLSNIFF_PATH], 
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                time.sleep(2)  # Give sslsniff time to start
                
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
                if sslsniff_proc is not None:
                    try:
                        sslsniff_proc.terminate()
                        sslsniff_proc.wait(timeout=5)
                    except Exception:
                        pass
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
                debug_print(f"Starting sslsniff with bpftime: LD_PRELOAD={SYSCALL_SERVER_PATH} {SSLSNIFF_PATH}")
                env = os.environ.copy()
                env["LD_PRELOAD"] = SYSCALL_SERVER_PATH
                sslsniff_proc = subprocess.Popen([SSLSNIFF_PATH],
                                                env=env,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                time.sleep(2)  # Give sslsniff time to start

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
                    if sslsniff_proc is not None:
                        sslsniff_proc.terminate()
                        sslsniff_proc.wait(timeout=5)
                except Exception as e:
                    debug_print(f"Error terminating processes: {e}")

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
            },
            "stats": run_stats,
            "results": results
        }, f, indent=2)
    
    print(f"\nResults saved to {filename}")

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
        run_baseline()
        run_kernel_sslsniff()
        run_bpftime_sslsniff()
        
        # Print and save results
        print_statistics()
        save_results()
        
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
