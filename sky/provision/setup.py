"""Setup dependencies & services during provisioning."""
from typing import List
import hashlib

from sky.utils import command_runner, subprocess_utils
from sky.provision import utils as provision_utils


def setup_dependencies(cluster_name: str, setup_commands: List[str],
                       ssh_runners: List[command_runner.SSHCommandRunner]):
    # TODO(suquark): log to files
    # compute the digest
    digests = []
    for cmd in setup_commands:
        digests.append(hashlib.sha256(cmd.encode()).digest())
    hasher = hashlib.sha256()
    for d in digests:
        hasher.update(d)
    digest = hasher.hexdigest()

    def _setup_node(runner: command_runner.SSHCommandRunner):
        for cmd in setup_commands:
            runner.run(cmd, stream_logs=False)

    with provision_utils.check_cache_hash_or_update(cluster_name,
                                                    'setup_dependencies',
                                                    digest) as updated:
        if updated:
            subprocess_utils.run_in_parallel(_setup_node, ssh_runners)


def start_ray(ssh_runners: List[command_runner.SSHCommandRunner],
              head_private_ip: str,
              check_ray_started: bool = False):
    if check_ray_started:
        returncode = ssh_runners[0].run('ray status', stream_logs=False)
        if returncode == 0:
            return

    ray_prlimit = (
        'which prlimit && for id in $(pgrep -f raylet/raylet); '
        'do sudo prlimit --nofile=1048576:1048576 --pid=$id || true; done;')

    ssh_runners[0].run('ray stop; ray start --disable-usage-stats --head '
                       '--port=6379 --object-manager-port=8076;' + ray_prlimit,
                       stream_logs=False)

    def _setup_ray_worker(runner: command_runner.SSHCommandRunner):
        # for cmd in config_from_yaml['worker_start_ray_commands']:
        #     cmd = cmd.replace('$RAY_HEAD_IP', ip_list[0][0])
        #     runner.run(cmd)
        runner.run(f'ray stop; ray start --disable-usage-stats '
                   f'--address={head_private_ip}:6379;' + ray_prlimit,
                   stream_logs=False)

    subprocess_utils.run_in_parallel(_setup_ray_worker, ssh_runners[1:])


def start_skylet(ssh_runner: command_runner.SSHCommandRunner):
    # "source ~/.bashrc" has side effects similar to
    # https://stackoverflow.com/questions/29709790/scripts-with-nohup-inside-dont-exit-correctly
    # This side effects blocks SSH from exiting. We address it by nesting
    # bash commands.
    ssh_runner.run(
        '(ps aux | grep -v nohup | grep -v grep | grep -q '
        '-- "python3 -m sky.skylet.skylet") || (bash -c \'source ~/.bashrc '
        '&& nohup python3 -m sky.skylet.skylet >> ~/.sky/skylet.log 2>&1 &\' '
        '&> /dev/null &)',
        stream_logs=False)
