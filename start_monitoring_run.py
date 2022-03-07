#!/usr/bin/env python3
import argparse
import collections
import concurrent.futures
import configparser
import datetime
import enum
import glob
import logging
import os
import queue
import shutil
import subprocess
import time

tb_analysis_dir = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "continuous_event_building",
    "SiWECAL-TB-analysis",
)
assert os.path.isdir(tb_analysis_dir), tb_analysis_dir

file_paths = dict(
    run_settings="Run_Settings.txt",
    default_config="monitoring.cfg",
    log_file="log_monitoring.log",
    masked_channels="masked_channels.txt",
    tb_analysis_dir=tb_analysis_dir,
)
monitoring_subfolders = dict(
    tmp_dir="tmp",
    converted_dir="converted",
    build_dir="build",
)
file_paths.update(**monitoring_subfolders)
my_paths = collections.namedtuple("Paths", file_paths.keys())(**file_paths)
get_root_str = (
    "source /cvmfs/sft.cern.ch/lcg/views/LCG_99/x86_64-centos7-gcc10-opt/setup.sh"
)


class Priority(enum.IntEnum):
    """For job scheduling. Lowest value is executed first."""

    MONITORING = 1
    EVENT_BUILDING = 2
    CONVERSION = 3
    DONE = 4


def create_directory_structure(run_output_dir):
    if not os.path.exists(run_output_dir):
        os.mkdir(run_output_dir)
    for subfolder in monitoring_subfolders.values():
        sub_path = os.path.join(run_output_dir, subfolder)
        if not os.path.exists(sub_path):
            os.mkdir(sub_path)


def cleanup_temporary(output_dir, logger):
    logger.warning(
        f"🧹The output directory {output_dir} already exists. "
        "This is ok and expected if you had already started (and aborted) "
        "a monitoring session for this run earlier. "
    )
    output_conf = os.path.join(
        output_dir, my_paths.log_file
    )  # my_paths.default_config)
    if not os.path.exists(output_conf):
        logger.error(
            "⛔Aborted. A previous run should have left behind "
            f"a logfile at {output_conf}. "
            # f"a config file at {output_conf}. "
            "As no such file was found, it is assumed that you specified "
            "the wrong directory. "
            "To nevertheless use this output directory it suffices to"
            " create an empty file with this name."
        )
        exit()

    # Move run-level files to a timestamped version, so they can be recreated.
    old_file_suffix = "_" + datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    for existing_file in [
        my_paths.default_config,
        # my_paths.log,  # Comment this out for now: I prefer appending to the logfile.
        my_paths.masked_channels,
    ]:
        existing_file = os.path.join(output_dir, existing_file)
        if os.path.isfile(existing_file):
            os.rename(
                existing_file, old_file_suffix.join(os.path.splitext(existing_file))
            )

    tmp_dir = os.path.join(output_dir, my_paths.tmp_dir)
    if os.path.isdir(tmp_dir):
        for tmp_file in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, tmp_file))


def configure_logging(logger, log_file=None):
    """TODO: Nicer formatting. Maybe different for console and file."""
    logger.setLevel("DEBUG")
    FORMAT = "%(asctime)s[%(levelname)-5.5s:%(name)s %(threadName)-50.50s] %(message)s"
    fmt = logging.Formatter(FORMAT, datefmt="%H:%M:%S")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt=fmt)
    logger.addHandler(console_handler)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(fmt=fmt)
        logger.addHandler(file_handler)

    time_now = datetime.datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
    logger.info(f"📃Logging to file {log_file} started at {time_now}.")


def log_unexpected_error_subprocess(logger, subprocess_return, add_context=""):
    logger.error(subprocess_return)
    logger.error(
        f"💣Unexpected error{add_context}. "
        "Maybe the lign above with return information "
        "from the subprocess can help to understand the issue?🙈"
    )


def guess_id_run(name, output_parent):
    """Ideally takes the number following `run_`. Else constructs a id_run."""
    # Best-case scenario: Find a number after the string `run_`.
    pos_run_prefix = name.lower().find("run_")
    if pos_run_prefix != -1:
        idx_start_number = pos_run_prefix + len("run_")
        if len(name) >= idx_start_number and name[idx_start_number].isdigit():
            for idx_end_number in range(idx_start_number, len(name)):
                if not name[idx_end_number].isdigit():
                    break
            else:
                idx_end_number += 1
            return int(name[idx_start_number:idx_end_number])

    # Next try: find the longest (then largest) number-string of at least length 3.
    numbers = [""]
    for s in name:
        if s.isdigit():
            numbers[-1] = numbers[-1] + s
        elif numbers[-1] != "":
            numbers.append("")
    longest_number = max(map(len, numbers))
    if longest_number >= 3:
        longest_numbers = (n for n in numbers if len(n) == longest_number)
        return max(map(int, longest_numbers))

    # Last resort: Use the number of monitored runs as id_run.
    return sum(
        os.path.isdir(os.path.join(output_parent, d)) for d in os.listdir(output_parent)
    )


class EcalMonitoring:
    def __init__(self, raw_run_folder, config_file):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.raw_run_folder = self._validate_raw_run_folder(raw_run_folder)
        self._validate_computing_environment()
        self._read_config(config_file)
        self.masked_channels = self.create_masking()

    def _validate_raw_run_folder(self, raw_run_folder):
        # Removes potential trailing backslash.
        # Otherwise, os.path.basename == "" later on.
        raw_run_folder = os.path.realpath(raw_run_folder)
        assert os.path.isdir(raw_run_folder), raw_run_folder
        run_settings = os.path.join(raw_run_folder, my_paths.run_settings)
        assert os.path.exists(run_settings), (
            run_settings + " must exist in the raw_run_folder."
        )
        return raw_run_folder

    def _validate_computing_environment(self):
        try:
            subprocess.run(["root", "--version"], capture_output=True)
        except FileNotFoundError:
            self.logger.error(
                "⛔Aborted. CERN root not available. "
                "💡Tipp: try the environment at\n" + get_root_str
            )
            exit()
        env_py_v = ""
        try:
            # This is not necessarily the python that runs this script, but
            # the one that will run the eventbuiling (and is linked with ROOT).
            ret = subprocess.run(["python", "--version"], capture_output=True)
            # Python2 writes version info to sys.stderr, Python3 to sys.stderr.
            env_py_v = ret.stdout + ret.stderr
            assert env_py_v.startswith(b"Python ") and env_py_v.endswith(
                b"\n"
            ), env_py_v
            py_v_list = list(map(int, env_py_v[len("Python ") : -1].split(b".")))
        except Exception as e:
            self.logger.error(f"env_py_v={env_py_v}")
            self.logger.exception(e)
            py_v_list = [2, 0, 0]
        if py_v_list[0] != 3:
            self.logger.warning(
                "🐌Using pyROOT with python2 for eventbuilding is not technically "
                f"forbidden, but discouraged for performance reasons: {env_py_v}. "
                "💡Tipp: try the environment at\n" + get_root_str
            )

    def _read_config(self, config_file):
        if not os.path.isabs(config_file):
            folder = os.path.dirname(os.path.abspath(__file__))
            config_file = os.path.join(folder, config_file)
        assert os.path.isfile(config_file), config_file
        config = configparser.ConfigParser()

        def get_with_fallback(section, key, default):
            config.set(section, key, config[section].get(key, default))
            return config[section][key]

        config.read(config_file)
        output_parent = get_with_fallback("monitoring", "output_parent", "data")
        if not os.path.exists(output_parent):
            os.mkdir(output_parent)
        output_name = os.path.basename(self.raw_run_folder)
        output_name = get_with_fallback("monitoring", "output_name", output_name)
        self.output_dir = os.path.abspath(os.path.join(output_parent, output_name))
        if os.path.exists(self.output_dir) and len(os.listdir(self.output_dir)) > 0:
            cleanup_temporary(self.output_dir, self.logger)
        create_directory_structure(self.output_dir)
        configure_logging(self.logger, os.path.join(self.output_dir, my_paths.log_file))
        self.max_workers = int(get_with_fallback("monitoring", "max_workers", "10"))
        assert self.max_workers >= 1, self.max_workers

        def ensure_calibration_exists(calib):
            file = os.path.abspath(config["eventbuilding"].get(calib))
            assert os.path.exists(file), file
            config["eventbuilding"][calib] = file
            return file

        self.eventbuilding_args = dict()
        for calib in [
            "pedestals_file",
            "mip_calibration_file",
            "pedestals_lg_file",
            "mip_calibration_lg_file",
        ]:
            self.eventbuilding_args[calib] = ensure_calibration_exists(calib)
        self.eventbuilding_args["w_config"] = config["eventbuilding"].getint("w_config")
        self.eventbuilding_args["min_slabs_hit"] = config["eventbuilding"].getint(
            "min_slabs_hit"
        )
        self.eventbuilding_args["cob_positions_string"] = config["eventbuilding"][
            "cob_positions_string"
        ]
        if "id_run" not in config["eventbuilding"]:
            config["eventbuilding"]["id_run"] = str(
                guess_id_run(output_name, output_parent)
            )
        self.eventbuilding_args["id_run"] = config["eventbuilding"].getint("id_run")

        with open(os.path.join(self.output_dir, my_paths.default_config), "w") as f:
            config.write(f)
        self.logger.info("Config file written.")
        return config

    def create_masking(self):
        tmp_run_settings = os.path.join(self.output_dir, my_paths.run_settings)
        shutil.copy(
            os.path.join(self.raw_run_folder, my_paths.run_settings),
            tmp_run_settings,
        )
        tmp_rs_name = os.path.splitext(tmp_run_settings)[0]
        root_macro_dir = os.path.join(my_paths.tb_analysis_dir, "SLBcommissioning")
        root_call = f'"test_read_masked_channels_summary.C(\\"{tmp_rs_name}\\")"'
        ret = subprocess.run(
            "root -b -l -q " + root_call,
            shell=True,
            capture_output=True,
            cwd=root_macro_dir,
        )
        output_lines = ret.stdout.split(b"\n")
        # Reading error as indicated by the root macro's output.
        root_macro_issue_stdout = b" dameyo - damedame"
        settings_file_not_read = output_lines[2] == root_macro_issue_stdout
        if ret.returncode != 0 or settings_file_not_read:
            log_unexpected_error_subprocess(self.logger, ret, " during create_masking")
            exit()
        assert not any(
            [line == root_macro_issue_stdout for line in output_lines]
        ), "This condition should be unreachable."
        masked_channels = os.path.join(self.output_dir, my_paths.masked_channels)
        os.rename(
            os.path.join(self.output_dir, tmp_rs_name + "_masked.txt"),
            masked_channels,
        )
        os.remove(tmp_run_settings)
        self.logger.debug(f"👏Channel masks written to {masked_channels}.")
        self.eventbuilding_args["masked_file"] = masked_channels
        return masked_channels

    def start_loop(self):
        self._largest_raw_dat = 0
        self._run_finished = False
        self._time_last_raw_check = 0
        self._time_last_job = time.time()
        job_queue = queue.PriorityQueue()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for i in range(self.max_workers):
                futures.append(executor.submit(self.find_and_do_job, job_queue))
                time.sleep(1)  # Head start for the startup bookkeeping.
            done, not_done = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
        self.logger.info(
            "💡If this script does not finish soon, there is an unexpected error. "
        )
        self.logger.info(
            "🛬The run has finished. The monitoring has treated all files. "
        )
        self._debug_future_returns(done, not_done, job_queue)

    def _debug_future_returns(self, done, not_done, job_queue):
        """Check the futures for issues. Added for debugging; should be fast."""
        had_exception = False
        for p in done:
            if p.exception() is not None:
                had_exception = True
                self.logger.error(
                    f"At least one thread raised an exception: {p.exception()}."
                )
                try:
                    p.result()
                except Exception as e:
                    self.logger.exception(e)
            else:
                if p.result() is not None:
                    self.logger.error(f"p.result()={p.result()}")
                    raise NotImplementedError
        assert len(not_done) == 0
        if not had_exception and not hasattr(self, "_stopped_gracefully"):
            job_queue.join()

    def find_and_do_job(self, job_queue):
        while True:
            # The try is not technically thread safe, but good enough here.
            try:
                assert job_queue.queue[0][0] < Priority.CONVERSION
            except (IndexError, AssertionError):
                self._look_for_new_raw(job_queue)

            all_done = self._run_finished and job_queue.empty()
            file_stop_gracefully = os.path.join(self.output_dir, "stop_monitoring")
            if all_done or os.path.exists(file_stop_gracefully):
                if not all_done:
                    if not hasattr(self, "_stopped_gracefully"):
                        self._stopped_gracefully = True
                        self.logger.info(
                            "🤝Graceful stopping granted before end of monitoring. "
                            f"This was requested by {file_stop_gracefully}."
                        )
                return

            try:
                priority, neg_id_dat, in_file = job_queue.get(timeout=2)
            except queue.Empty:
                continue
            if priority == Priority.CONVERSION:
                converted_file = self.convert_to_root(in_file)
                job_queue.put((Priority.EVENT_BUILDING, neg_id_dat, converted_file))
            elif priority == Priority.EVENT_BUILDING:
                build_file = self.run_eventbuilding(in_file, -neg_id_dat)
                job_queue.put((Priority.MONITORING, neg_id_dat, build_file))
            elif priority == Priority.MONITORING:
                print("#TODO")
            else:
                raise NotImplementedError(priority)
            job_queue.task_done()
            self._time_last_job = time.time()
            self.logger.debug(f"🌟One task done: {priority.name}, {in_file}")

    def _look_for_new_raw(self, job_queue):
        if self._run_finished:
            return
        delta_t_daq_output_checks = 2  # in seconds.
        if time.time() - self._time_last_raw_check < delta_t_daq_output_checks:
            if job_queue.empty():
                time.sleep(delta_t_daq_output_checks)
            return
        self._time_last_raw_check = time.time()
        dat_pattern = os.path.join(self.raw_run_folder, "*.dat_[0-9][0-9][0-9][0-9]")
        dat_files = sorted(glob.glob(dat_pattern))
        path_start = dat_files[-1][:-4]
        new_largest_dat = int(dat_files[-1][-4:])
        for i in range(self._largest_raw_dat, new_largest_dat):
            path = path_start + f"{i:04}"
            if os.path.exists(path):
                job_queue.put((Priority.CONVERSION, -i, path))
        self._largest_raw_dat = new_largest_dat
        file_run_finished = os.path.join(self.raw_run_folder, "hitsHistogram.txt")
        self._run_finished = os.path.exists(file_run_finished)
        if self._run_finished:
            job_queue.put((Priority.CONVERSION, -new_largest_dat, dat_files[-1]))
            self.logger.info(
                "🏃The run has finished. " "Monitoring will try to catch up now."
            )
        self._alert_is_idle(file_run_finished)

    def _alert_is_idle(self, file_run_finished, seconds_before_alert=60):
        time_without_jobs = time.time() - self._time_last_job
        n_idle_infos = getattr(self, "_n_idle_infos", 1)
        if time_without_jobs < seconds_before_alert * n_idle_infos:
            return
        self._n_idle_infos = n_idle_infos + 1

        file_suppress_idle_info = os.path.join(self.output_dir, "suppress_idle_info")
        if os.path.exists(file_suppress_idle_info):
            return

        self.logger.info(
            "⌛🤷Already waiting for new jobs since "
            f"{int(time_without_jobs)} seconds. "
            "By now we would have expected to find the file that "
            f"indicates the end of the run: {file_run_finished}. "
        )
        self.logger.info(
            "💡Tipp: To exit this inifinite loop gracefully, and perform "
            "the end-of run computations, create a dummy version of that file. "
            f"To supress this info, create the file {file_suppress_idle_info}. "
        )

    def convert_to_root(self, raw_file_path):
        raw_file_name = os.path.basename(raw_file_path)
        converted_name = "converted_" + raw_file_name + ".root"
        out_path = os.path.join(self.output_dir, my_paths.converted_dir, converted_name)
        if os.path.exists(out_path):
            return out_path
        tmp_path = os.path.join(self.output_dir, my_paths.tmp_dir, converted_name)
        in_path = os.path.join(self.output_dir, self.raw_run_folder, raw_file_name)

        root_macro_dir = os.path.join(my_paths.tb_analysis_dir, "converter_SLB")
        root_call = f'"ConvertDataSL.cc(\\"{in_path}\\", false, \\"{tmp_path}\\")"'
        ret = subprocess.run(
            "root -b -l -q " + root_call,
            shell=True,
            capture_output=True,
            cwd=root_macro_dir,
        )
        if ret.returncode != 0 or ret.stderr != b"":
            log_unexpected_error_subprocess(self.logger, ret, " during convert_to_root")
            exit()
        os.rename(tmp_path, out_path)
        return out_path

    def run_eventbuilding(self, convered_path, id_dat):
        converted_name = os.path.basename(convered_path)
        build_name = converted_name.replace("converted_", "build_")
        out_path = os.path.join(self.output_dir, my_paths.build_dir, build_name)
        if os.path.exists(out_path):
            return out_path
        tmp_path = os.path.join(self.output_dir, my_paths.tmp_dir, build_name)
        in_path = os.path.join(self.output_dir, my_paths.converted_dir, converted_name)

        builder_dir = os.path.join(my_paths.tb_analysis_dir, "eventbuilding")
        args = f" {in_path} --out_file_name {tmp_path}"
        self.eventbuilding_args["id_dat"] = int(id_dat)
        for k, v in self.eventbuilding_args.items():
            args += f" --{k} {v}"
        args += " --no_progress_info"
        ret = subprocess.run(
            "./build_events.py" + args,
            shell=True,
            capture_output=True,
            cwd=builder_dir,
        )
        if ret.returncode != 0 or ret.stderr != b"":
            log_unexpected_error_subprocess(
                self.logger, ret, " during run_eventbuilding"
            )
            exit()
        os.rename(tmp_path, out_path)
        return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the event-based monitoring loop from SiW-ECAL DAQ files.",
    )
    parser.add_argument("raw_run_folder", help="Folder of the run to be monitored.")
    parser.add_argument(
        "-c",
        "--config_file",
        default=my_paths.default_config,
        help=f"If relative path, then relative to {__file__}",
    )
    monitoring = EcalMonitoring(**vars(parser.parse_args()))
    monitoring.start_loop()
    exit()
