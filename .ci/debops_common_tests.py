#!/usr/bin/env python

import os
import sys

import ci_lib


TMP = ci_lib.TempDir(prefix='mitogen_ci_debops')
project_dir = os.path.join(TMP.path, 'project')
vars_path = 'ansible/inventory/group_vars/debops_all_hosts.yml'
inventory_path = 'ansible/inventory/hosts'
docker_hostname = ci_lib.get_docker_hostname()


with ci_lib.Fold('docker_setup'):
    containers = ci_lib.container_specs(
        ['debian*2'],
        base_port=2700,
        name_template='debops-target-%(distro)s-%(index)d',
    )
    ci_lib.start_containers(containers)


with ci_lib.Fold('job_setup'):
    os.chmod(ci_lib.TESTS_SSH_PRIVATE_KEY_FILE, int('0600', 8))
    ci_lib.run('debops-init %s', project_dir)
    os.chdir(project_dir)

    ansible_strategy_plugin = "{}/ansible_mitogen/plugins/strategy".format(ci_lib.GIT_ROOT)

    with open('.debops.cfg', 'w') as fp:
        fp.write(
            "[ansible defaults]\n"
            "strategy_plugins = {}\n"
            "strategy = mitogen_linear\n"
            .format(ansible_strategy_plugin)
        )

    with open(vars_path, 'w') as fp:
        fp.write(
            "ansible_python_interpreter: /usr/bin/python2.7\n"
            "\n"
            "ansible_user: mitogen__has_sudo_pubkey\n"
            "ansible_become_pass: has_sudo_pubkey_password\n"
            "ansible_ssh_private_key_file: %s\n"
            "\n"
            # Speed up slow DH generation.
            "dhparam__bits: ['128', '64']\n"
            % (ci_lib.TESTS_SSH_PRIVATE_KEY_FILE,)
        )

    with open(inventory_path, 'a') as fp:
        fp.writelines(
            '%(name)s '
            'ansible_host=%(hostname)s '
            'ansible_port=%(port)d '
            'ansible_python_interpreter=%(python_path)s '
            '\n'
            % container
            for container in containers
        )

    ci_lib.dump_file('ansible/inventory/hosts')

    # Now we have real host key checking, we need to turn it off
    os.environ['ANSIBLE_HOST_KEY_CHECKING'] = 'False'


interesting = ci_lib.get_interesting_procs()

with ci_lib.Fold('first_run'):
    ci_lib.run('debops common %s', ' '.join(sys.argv[1:]))

ci_lib.check_stray_processes(interesting, containers)


with ci_lib.Fold('second_run'):
    ci_lib.run('debops common %s', ' '.join(sys.argv[1:]))

ci_lib.check_stray_processes(interesting, containers)
