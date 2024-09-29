import argparse
import json
import logging
from pathlib import Path
import re
from datetime import datetime as dt, timedelta
import shutil
import subprocess
import sys
import calendar
import time
import os
import dotenv
import gc  # For garbage collection

dotenv.load_dotenv()

def valid_date(s):
    try:
        return dt.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        msg = f"Not a valid date: {s}. Please use the format `YYYY-MM-DDThh:mm:ss`."
        raise argparse.ArgumentTypeError(msg)

def parse_time_range(s):
    UNITS = "d", "m", "y"
    match = re.match(r'(\d+)(\w+)', s)
    try:
        num, unit = match.groups()
        unit = unit.lower()
    except BaseException:
        raise argparse.ArgumentTypeError(f"Could not parse `{s}`: should be in format `2d|1m|6m|1y`")
    if unit[0] not in UNITS:
        raise argparse.ArgumentTypeError(f"{s} -- unrecognized time unit: {unit}")
    if int(num) == 0:
        raise argparse.ArgumentTypeError(f"Prefix cannot be zero or negative: {s}")
    return s

def get_time_ranges(s, earliest: dt, latest: dt) -> list[tuple[dt, dt]]:
    ONEDAY = timedelta(days=1)
    ONESEC = timedelta(seconds=1)
    match = re.match(r'(\d+)(\w+)', s)
    num, unit = match.groups()
    num = int(num)
    output = []
    unit = unit[0]
    hi = earliest
    while hi < latest:
        lo = hi
        for _ in range(num):
            if unit == 'm':
                _, days_in_month = calendar.monthrange(hi.year, hi.month)
                hi = hi.replace(day=days_in_month, hour=23, minute=59, second=59)
            if unit == 'd':
                hi += ONEDAY
            if unit == 'y':
                hi = dt(hi.year, 12, 31, 23, 59, 59)
            hi += ONESEC
        hi -= ONEDAY
        hi = hi.replace(hour=23, minute=59, second=59)
        if hi >= latest:
            hi = latest
        output.append((unit, lo, hi))
        hi += ONESEC
    return output

def parse_args():
    parser = argparse.ArgumentParser(description="Helper script for converting CVE and CPE data to STIX format.", allow_abbrev=True)
    group = parser.add_argument_group()
    group.add_argument("--run_cve2stix", help="Set to true to run CVE to STIX conversion", action="store_true")
    group.add_argument("--run_cpe2stix", help="Set to true to run CPE to STIX conversion", action="store_true")
    parser.add_argument("--last_modified_earliest", help="Earliest date for last modified filter", metavar="YYYY-MM-DDThh:mm:ss", required=True, type=valid_date)
    latest = parser.add_argument("--last_modified_latest", help="Latest date for last modified filter", metavar="YYYY-MM-DDThh:mm:ss", required=True, type=valid_date)
    parser.add_argument("--file_time_range", help="Time range for file processing (e.g., 1m)", default="1m", type=parse_time_range)
    
    args = parser.parse_args()

    if not (args.run_cve2stix or args.run_cpe2stix):
        parser.error("At least one of --run_cve2stix and --run_cpe2stix must be set")

    if args.last_modified_latest < args.last_modified_earliest:
        raise argparse.ArgumentError(latest, "--last_modified_latest must not be earlier than --last_modified_earliest")

    return args

CELERY_PROCESSES = []  # Store subprocesses here for later cleanup

def start_celery(path: str, cwd=".", env=None):
    logging.info(f"Starting celery: {path}")
    args = ["celery", "-A", path, "--workdir", cwd, "worker", "--loglevel", "info", "--purge"]
    p = subprocess.Popen(args, stdout=sys.stdout, stderr=sys.stderr)
    
    logging.info(f"Waiting 10 seconds for celery to initialize")
    time.sleep(10)  # Allow time for celery to initialize
    CELERY_PROCESSES.append(p)  # Add process to list for later cleanup
    return p

def kill_all_processes():
    """Terminate all subprocesses and ensure they are cleaned up."""
    for p in CELERY_PROCESSES:
        logging.info(f"Killing celery worker with PID: {p.pid}")
        p.kill()  # Terminate the subprocess
        p.wait()  # Wait for the process to fully terminate
    CELERY_PROCESSES.clear()  # Clear the list after all processes are killed

def main():
    args = parse_args()

    # Setting environment variables
    os.environ.update(dict(
        CVE_LAST_MODIFIED_EARLIEST=args.last_modified_earliest.strftime("%Y-%m-%dT%H:%M:%S"),
        CVE_LAST_MODIFIED_LATEST=args.last_modified_latest.strftime("%Y-%m-%dT%H:%M:%S"),
    ))

    sys.path.append("cpe2stix")
    sys.path.append("cve2stix")

    import cpe2stix.main as cpe2stix
    import cve2stix.main as cve2stix

    cve_results_per_page = os.getenv("CVE2STIX_RESULTS_PER_PAGE", "500")
    cpe_results_per_page = os.getenv("CPE2STIX_RESULTS_PER_PAGE", "10000")

    PARENT_PATH = Path("./output").absolute()
    OBJECTS_PARENT = PARENT_PATH / "objects"
    BUNDLE_PATH = PARENT_PATH / "bundles"

    shutil.rmtree(PARENT_PATH, ignore_errors=True)

    for time_unit, start_date, end_date in get_time_ranges(args.file_time_range, args.last_modified_earliest, args.last_modified_latest):
        start_day, end_day = start_date.strftime('%Y_%m_%d-%H_%M_%S'), end_date.strftime('%Y_%m_%d-%H_%M_%S')
        subdir = start_date.strftime('%Y-%m') if time_unit == 'd' else start_date.strftime('%Y')

        if args.run_cve2stix:
            file_system = OBJECTS_PARENT / f"cve_objects-{start_day}-{end_day}"
            file_system.mkdir(parents=True, exist_ok=True)
            
            cprocess = start_celery("cve2stix.celery", "cve2stix")
            bundle_name = f"cve/{subdir}/cve-bundle-{start_day}-{end_day}.json"
            (BUNDLE_PATH / bundle_name).parent.mkdir(parents=True, exist_ok=True)
            task = cve2stix.main(
                filename=bundle_name,
                config=cve2stix.Config(
                    start_date=start_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    end_date=end_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    stix2_objects_folder=str(file_system),
                    file_system=str(file_system),
                    stix2_bundles_folder=str(BUNDLE_PATH),
                    results_per_page=int(cve_results_per_page),
                    nvd_api_key=os.getenv("NVD_API_KEY")
                ),
            )

            # Wait for all tasks to complete before proceeding
            task.get()  # Ensure all tasks are completed before moving to the next iteration
            cprocess.kill()  # Now it is safe to kill the celery worker

        if args.run_cpe2stix:
            file_system = OBJECTS_PARENT / f"cpe_objects-{start_day}-{end_day}"
            file_system.mkdir(parents=True, exist_ok=True)

            cprocess = start_celery("cpe2stix.celery", "cpe2stix")
            bundle_name = f"cpe/{subdir}/cpe-bundle-{start_day}-{end_day}.json"
            (BUNDLE_PATH / bundle_name).parent.mkdir(parents=True, exist_ok=True)
            task = cpe2stix.main(
                filename=bundle_name,
                config=cpe2stix.Config(
                    start_date=start_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    end_date=end_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    stix2_objects_folder=str(file_system),
                    file_system=str(file_system),
                    stix2_bundles_folder=str(BUNDLE_PATH),
                    results_per_page=int(cpe_results_per_page),
                    nvd_api_key=os.getenv("NVD_API_KEY")
                ),
            )

            # Wait for all tasks to complete before proceeding
            task.get()  # Ensure all tasks are completed before moving to the next iteration
            cprocess.kill()  # Now it is safe to kill the celery worker

        # Cleanup after each iteration
        shutil.rmtree(file_system, ignore_errors=True)
        gc.collect()  # Force garbage collection to free memory

    # Clean up objects directory if KEEP_OBJECTS_DIR is not set
    if os.getenv('KEEP_OBJECTS_DIR') != "true":
        shutil.rmtree(OBJECTS_PARENT, ignore_errors=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
    finally:
        logging.info("Cleaning up all Celery processes.")
        kill_all_processes()  # Ensure that all subprocesses are killed off
