# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.


"""Signal the run-build process to dump its run file and print it"""


import dataclasses
import functools
import json
import logging
import os
import pathlib
import random
import signal
import sys
import time

import lib.litani
import lib.litani_report
import lib.pid_file
import lib.util


"""When run-build receives this Unix signal, it will write the run file"""
DUMP_SIGNAL = signal.SIGUSR1
_DUMPED_RUN = "dumped-run.json"


def add_subparser(subparsers):
    dump_run_pars = subparsers.add_parser("dump-run")
    dump_run_pars.set_defaults(func=dump_run)
    for arg in [{
            "flags": ["-r", "--retries"],
            "metavar": "N",
            "type": lib.util.non_negative_int,
            "default": 10,
            "help":
                "how many times to retry loading the run in 1 second intervals"
                " (Default: %(default)s)"
        }, {
            "flags": ["-o", "--out-file"],
            "metavar": "F",
            "type": lib.util._non_directory_path,
            "help": "Output file to dump run file in"
    }]:
        flags = arg.pop("flags")
        dump_run_pars.add_argument(*flags, **arg)


@dataclasses.dataclass
class BackoffSleeper:
    jitter: float
    duration: float = 0.2
    multiplier: int = 2


    def sleep(self):
        time.sleep(self.duration)

        self.duration += self.jitter
        self.duration *= self.multiplier
        self.jitter *= self.multiplier



class InconsistentRunError(RuntimeError):
    pass



def run_consistent_to_job(run, job_id):
    """True iff the reverse-dependencies of job_id are marked as complete"""

    out_to_status = {}
    job_ins = []
    found = False

    for pipe in run["pipelines"]:
        for stage in pipe["ci_stages"]:
            for job in stage["jobs"]:
                current_id = job["wrapper_arguments"]["job_id"]
                if current_id == job_id:
                    found = True
                    job_ins = job["wrapper_arguments"]["inputs"] or []

                for out in job["wrapper_arguments"]["outputs"] or []:
                    if out in out_to_status:
                        logging.warning(
                            "Two jobs share an output '%s'. Jobs: %s and %s",
                            out, out_to_status[out]["job_id"], current_id)
                    out_to_status[out] = {
                        "job_id": current_id,
                        "complete": job["complete"],
                    }
    if not found:
        logging.error(
            "Could not find job with ID '%s' in run", job_id)
        raise InconsistentRunError()

    for inputt in job_ins:
        if inputt not in out_to_status:
            continue
        if not out_to_status[inputt]["complete"]:
            logging.debug(
                "Run inconsistent: job '%s' is a reverse-dependency of "
                "job '%s' through input '%s', but the reverse-dependency "
                "job is not marked as complete.",
                out_to_status[inputt]["job_id"], job_id, inputt)
            raise InconsistentRunError()
    return True


def get_run_checker():
    # If we're being run from inside a litani job, we should check that the run
    # is consistent with the job before printing out the run. Otherwise, no
    # check is required, just print out the run.

    parent_job_id = os.getenv(lib.litani.ENV_VAR_JOB_ID)
    if parent_job_id:
        return functools.partial(run_consistent_to_job, job_id=parent_job_id)
    return lambda run: True



def _exit_success(run, out_file):
    if out_file:
        with lib.litani.atomic_write(out_file) as handle:
            print(json.dumps(run, indent=2), file=handle)
    else:
        print(json.dumps(run, indent=2))
    sys.exit(0)


def _exit_error():
    print(json.dumps(None))
    sys.exit(0)


def _try_dump_run(cache_dir, pid, sleeper, check_run, out_file):
    try:
        os.kill(pid, DUMP_SIGNAL)
    except ProcessLookupError:
        logging.debug("pid %s does not match to a running process", pid)
        latest_run_file = cache_dir / lib.litani.RUN_FILE
        try:
            with open(latest_run_file) as handle:
                latest_run = json.load(handle)
            check_run(latest_run)
            _exit_success(latest_run, out_file)
        except (
            FileNotFoundError, json.decoder.JSONDecodeError,
            InconsistentRunError):
            logging.warning("Could not find run.json inside the latest run")
            _exit_error()
    try:
        with open(cache_dir / _DUMPED_RUN) as handle:
            run = json.load(handle)
        check_run(run)
        _exit_success(run, out_file)
    except (
            FileNotFoundError, json.decoder.JSONDecodeError,
            InconsistentRunError):
        sleeper.sleep()


async def dump_run(args):
    random.seed()

    try:
        pid = lib.pid_file.read()
    except FileNotFoundError:
        _exit_error()

    cache_dir = lib.litani.get_cache_dir()

    sleeper = BackoffSleeper(jitter=random.random())
    check_run = get_run_checker()
    out_file = args.out_file if args.out_file else None

    if args.retries:
        for _ in range(args.retries):
            _try_dump_run(cache_dir, pid, sleeper, check_run, out_file)
    else:
        while True:
            _try_dump_run(cache_dir, pid, sleeper, check_run, out_file)
    _exit_error()



@dataclasses.dataclass
class DumpRunSignalHandler:
    """Signal handler matching the API of the argument to signal.signal()"""

    cache_dir: pathlib.Path


    def __call__(self, _signum, _frame):
        run = lib.litani_report.get_run_data(self.cache_dir)
        with lib.litani.atomic_write(
                self.cache_dir / _DUMPED_RUN) as handle:
            print(json.dumps(run, indent=2), file=handle)
