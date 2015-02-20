import os
import sys
import json
import time
import shutil
import pprint
import weakref
import logging
import os.path
import argparse
import traceback
import subprocess
import contextlib


import ssh_runner
import io_scenario
from utils import log_error
from rest_api import add_test
from itest import IOPerfTest, run_test_iter
from starts_vms import nova_connect, create_vms_mt, clear_all

try:
    import rally_runner
except ImportError:
    rally_runner = None


logger = logging.getLogger("io-perf-tool")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
logger.addHandler(ch)

log_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
formatter = logging.Formatter(log_format,
                              "%H:%M:%S")
ch.setFormatter(formatter)


def run_io_test(tool,
                script_args,
                test_runner,
                keep_temp_files=False):

    files_dir = os.path.dirname(io_scenario.__file__)

    path = 'iozone' if 'iozone' == tool else 'fio'
    src_testtool_path = os.path.join(files_dir, path)

    obj = IOPerfTest(script_args,
                     src_testtool_path,
                     None,
                     keep_temp_files)

    return test_runner(obj)


class FileWrapper(object):
    def __init__(self, fd, conn):
        self.fd = fd
        self.channel_wr = weakref.ref(conn)

    def read(self):
        return self.fd.read()

    @property
    def channel(self):
        return self.channel_wr()


class LocalConnection(object):
    def __init__(self):
        self.proc = None

    def exec_command(self, cmd):
        PIPE = subprocess.PIPE
        self.proc = subprocess.Popen(cmd,
                                     shell=True,
                                     stdout=PIPE,
                                     stderr=PIPE,
                                     stdin=PIPE)
        res = (self.proc.stdin,
               FileWrapper(self.proc.stdout, self),
               self.proc.stderr)

        return res

    def recv_exit_status(self):
        return self.proc.wait()

    def open_sftp(self):
        return self

    def close(self):
        pass

    def put(self, localfile, remfile):
        return shutil.copy(localfile, remfile)

    def mkdir(self, remotepath, mode):
        os.mkdir(remotepath)
        os.chmod(remotepath, mode)

    def chmod(self, remotepath, mode):
        os.chmod(remotepath, mode)

    def copytree(self, src, dst):
        shutil.copytree(src, dst)


def get_local_runner(clear_tmp_files=True):
    def closure(obj):
        res = []
        obj.set_result_cb(res.append)
        test_iter = run_test_iter(obj,
                                  LocalConnection())
        next(test_iter)

        with log_error("!Run test"):
            next(test_iter)
        return res

    return closure


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run disk io performance test")

    parser.add_argument("tool_type", help="test tool type",
                        choices=['iozone', 'fio'])

    parser.add_argument("-l", dest='extra_logs',
                        action='store_true', default=False,
                        help="print some extra log info")

    parser.add_argument("-o", "--io-opts", dest='io_opts',
                        help="cmd line options for io.py")

    parser.add_argument("-f", "--io-opts-file", dest='io_opts_file',
                        type=argparse.FileType('r'), default=None,
                        help="file with cmd line options for io.py")

    parser.add_argument("-t", "--test-directory", help="directory with test",
                        dest="test_directory", required=True)

    parser.add_argument("--max-preparation-time", default=300,
                        type=int, dest="max_preparation_time")

    parser.add_argument("-b", "--build-info", default=None,
                        dest="build_name")

    parser.add_argument("-d", "--data-server-url", default=None,
                        dest="data_server_url")

    parser.add_argument("-n", "--lab-name", default=None,
                        dest="lab_name")

    parser.add_argument("--create-vms-opts", default=None,
                        help="Creating vm's before run ssh runner",
                        dest="create_vms_opts")

    parser.add_argument("-k", "--keep", default=False,
                        help="keep temporary files",
                        dest="keep_temp_files", action='store_true')

    choices = ["local", "ssh"]

    if rally_runner is not None:
        choices.append("rally")

    parser.add_argument("--runner", required=True,
                        choices=choices, help="runner type")

    parser.add_argument("--runner-extra-opts", default=None,
                        dest="runner_opts", help="runner extra options")

    return parser.parse_args(argv)


def format_measurements_stat(res):
    if len(res) != 0:
        bw_mean = 0.0
        for measurement in res:
            bw_mean += measurement["bw_mean"]

        bw_mean /= len(res)

        it = ((bw_mean - measurement["bw_mean"]) ** 2 for measurement in res)
        bw_dev = sum(it) ** 0.5

        meta = res[0]['__meta__']

        sync = meta['sync']
        direct = meta['direct_io']

        if sync and direct:
            ss = "d+"
        elif sync:
            ss = "s"
        elif direct:
            ss = "d"
        else:
            ss = "a"

        key = "{0} {1} {2} {3}k".format(meta['action'], ss,
                                        meta['concurence'],
                                        meta['blocksize'])

        data = json.dumps({key: (int(bw_mean), int(bw_dev))})

        return data


def get_io_opts(io_opts_file, io_opts):
    if io_opts_file is not None and io_opts is not None:
        print "Options --io-opts-file and --io-opts can't be " + \
            "provided same time"
        exit(1)

    if io_opts_file is None and io_opts is None:
        print "Either --io-opts-file or --io-opts should " + \
            "be provided"
        exit(1)

    if io_opts_file is not None:
        io_opts = []

        opt_lines = io_opts_file.readlines()
        opt_lines = [i for i in opt_lines if i != "" and not i.startswith("#")]

        for opt_line in opt_lines:
            if opt_line.strip() != "":
                io_opts.append([opt.strip()
                               for opt in opt_line.strip().split(" ")
                               if opt.strip() != ""])
    else:
        io_opts = [[opt.strip()
                   for opt in io_opts.split(" ")
                   if opt.strip() != ""]]

    if len(io_opts) == 0:
        print "Can't found parameters for io. Check" + \
            "--io-opts-file or --io-opts options"
        exit(1)

    return io_opts


def format_result(res):
    data = "\n{0}\n".format("=" * 80)
    data += pprint.pformat(res) + "\n"
    data += "{0}\n".format("=" * 80)
    templ = "{0}\n\n====> {1}\n\n{2}\n\n"
    return templ.format(data, format_measurements_stat(res), "=" * 80)


@contextlib.contextmanager
def start_test_vms(opts):
    create_vms_opts = {}
    for opt in opts.split(","):
        name, val = opt.split("=", 1)
        create_vms_opts[name] = val

    user = create_vms_opts.pop("user")
    key_file = create_vms_opts.pop("key_file")
    aff_group = create_vms_opts.pop("aff_group", None)
    raw_count = create_vms_opts.pop("count", "x1")

    logger.debug("Connection to nova")
    nova = nova_connect()

    if raw_count.startswith("x"):
        logger.debug("Getting amount of compute services")
        count = len(nova.services.list(binary="nova-compute"))
        count *= int(raw_count[1:])
    else:
        count = int(raw_count)

    if aff_group is not None:
        scheduler_hints = {'group': aff_group}
    else:
        scheduler_hints = None

    create_vms_opts['scheduler_hints'] = scheduler_hints

    logger.debug("Will start {0} vms".format(count))

    try:
        ips = [i[0] for i in create_vms_mt(nova, count, **create_vms_opts)]

        uris = ["{0}@{1}::{2}".format(user, ip, key_file) for ip in ips]

        yield uris
    except:
        traceback.print_exc()
    finally:
        logger.debug("Clearing")
        clear_all(nova)


def main(argv):
    opts = parse_args(argv)

    if opts.extra_logs:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)

    io_opts = get_io_opts(opts.io_opts_file, opts.io_opts)

    if opts.runner == "rally":
        logger.debug("Use rally runner")
        for script_args in io_opts:

            cmd_line = " ".join(script_args)
            logger.debug("Run test with {0!r} params".format(cmd_line))

            runner = rally_runner.get_rally_runner(
                files_dir=os.path.dirname(io_scenario.__file__),
                rally_extra_opts=opts.runner_opts.split(" "),
                max_preparation_time=opts.max_preparation_time,
                keep_temp_files=opts.keep_temp_files)

            res = run_io_test(opts.tool_type,
                              script_args,
                              runner,
                              opts.keep_temp_files)
            logger.debug(format_result(res))

    elif opts.runner == "local":
        logger.debug("Run on local computer")
        try:
            for script_args in io_opts:
                cmd_line = " ".join(script_args)
                logger.debug("Run test with {0!r} params".format(cmd_line))
                runner = get_local_runner(opts.keep_temp_files)
                res = run_io_test(opts.tool_type,
                                  script_args,
                                  runner,
                                  opts.keep_temp_files)
                logger.debug(format_result(res))
        except:
            traceback.print_exc()
            return 1

    elif opts.runner == "ssh":
        logger.debug("Use ssh runner")

        uris = []

        if opts.create_vms_opts is not None:
            vm_context = start_test_vms(opts.create_vms_opts)
            uris += vm_context.__enter__()
        else:
            vm_context = None

        if opts.runner_opts is not None:
            uris += opts.runner_opts.split(";")

        if len(uris) == 0:
            logger.critical("You need to provide at least" +
                            " vm spawn params or ssh params")
            return 1

        try:
            for script_args in io_opts:
                cmd_line = " ".join(script_args)
                logger.debug("Run test with {0!r} params".format(cmd_line))
                latest_start_time = opts.max_preparation_time + time.time()
                runner = ssh_runner.get_ssh_runner(uris,
                                                   latest_start_time,
                                                   opts.keep_temp_files)
                res = run_io_test(opts.tool_type,
                                  script_args,
                                  runner,
                                  opts.keep_temp_files)
                logger.debug(format_result(res))

        except:
            traceback.print_exc()
            return 1
        finally:
            if vm_context is not None:
                vm_context.__exit__()
                logger.debug("Clearing")

    if opts.data_server_url:
        result = json.loads(format_measurements_stat(res))
        result['name'] = opts.build_name
        add_test(opts.build_name, result, opts.data_server_url)

    return 0


if __name__ == '__main__':
    exit(main(sys.argv[1:]))
