# Copyright 2019, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import os
import signal
import threading

try:
    import setproctitle
except ImportError:
    setproctitle = None

import mitogen.core
import ansible_mitogen.affinity
import ansible_mitogen.loaders
import ansible_mitogen.mixins
import ansible_mitogen.process

import ansible.executor.process.worker
import ansible.template
import ansible.utils.sentinel
import ansible.playbook.play_context
import ansible.plugins.loader


def _patch_awx_callback():
    """
    issue #400: AWX loads a display callback that suffers from thread-safety
    issues. Detect the presence of older AWX versions and patch the bug.
    """
    # AWX uses sitecustomize.py to force-load this package. If it exists, we're
    # running under AWX.
    try:
        import awx_display_callback.events
    except ImportError:
        return

    if hasattr(awx_display_callback.events.EventContext(), '_local'):
        # Patched version.
        return

    def patch_add_local(self, **kwargs):
        tls = vars(self._local)
        ctx = tls.setdefault('_ctx', {})
        ctx.update(kwargs)

    awx_display_callback.events.EventContext._local = threading.local()
    awx_display_callback.events.EventContext.add_local = patch_add_local

_patch_awx_callback()


def wrap_action_loader__get_with_context(name, *args, **kwargs):
    """
    While the mitogen strategy is active, trap action_loader.get_with_context()
    calls, augmenting any fetched class with ActionModuleMixin, which replaces
    various helper methods inherited from ActionBase with implementations that
    avoid the use of shell fragments wherever possible.

    This is used instead of static subclassing as it generalizes to third party
    action plugins outside the Ansible tree.
    """
    get_kwargs = {'class_only': True}
    if name in ('fetch',):
        name = 'mitogen_' + name
    get_kwargs['collection_list'] = kwargs.pop('collection_list', None)

    (klass, context) = ansible_mitogen.loaders.action_loader__get_with_context(
        name,
        **get_kwargs
    )

    if klass:
        bases = (ansible_mitogen.mixins.ActionModuleMixin, klass)
        adorned_klass = type(str(name), bases, {})
        if kwargs.get('class_only'):
            return ansible.plugins.loader.get_with_context_result(
                adorned_klass,
                context
            )

        return ansible.plugins.loader.get_with_context_result(
            adorned_klass(*args, **kwargs),
            context
        )

    return ansible.plugins.loader.get_with_context_result(None, context)


REDIRECTED_CONNECTION_PLUGINS = (
    'buildah',
    'docker',
    'kubectl',
    'jail',
    'local',
    'lxc',
    'lxd',
    'machinectl',
    'podman',
    'setns',
    'ssh',
)


def wrap_connection_loader__get_with_context(name, *args, **kwargs):
    """
    While a Mitogen strategy is active, rewrite
    connection_loader.get_with_context() calls for some transports into
    requests for a compatible Mitogen transport.
    """
    is_play_using_mitogen_connection = None
    if len(args) > 0 and isinstance(args[0], ansible.playbook.play_context.PlayContext):
        play_context = args[0]
        is_play_using_mitogen_connection = play_context.connection in REDIRECTED_CONNECTION_PLUGINS

    # assume true if we're not in a play context since we're using a Mitogen strategy
    if is_play_using_mitogen_connection is None:
        is_play_using_mitogen_connection = True

    redirect_connection = name in REDIRECTED_CONNECTION_PLUGINS and is_play_using_mitogen_connection
    if redirect_connection:
        name = 'mitogen_' + name

    return ansible_mitogen.loaders.connection_loader__get_with_context(name, *args, **kwargs)


def wrap_worker__run(self):
    """
    While a Mitogen strategy is active, trap WorkerProcess.run() calls and use
    the opportunity to set the worker's name in the process list and log
    output, activate profiling if requested, and bind the worker to a specific
    CPU.
    """
    if setproctitle:
        setproctitle.setproctitle('worker:%s task:%s' % (
            self._host.name,
            self._task.action,
        ))

    # Ignore parent's attempts to murder us when we still need to write
    # profiling output.
    if mitogen.core._profile_hook.__name__ != '_profile_hook':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    ansible_mitogen.logging.set_process_name('task')
    ansible_mitogen.affinity.policy.assign_worker()
    return mitogen.core._profile_hook('WorkerProcess',
        lambda: worker__run(self)
    )


class AnsibleWrappers(object):
    """
    Manage add/removal of various Ansible runtime hooks.
    """
    def _add_plugin_paths(self):
        """
        Add the Mitogen plug-in directories to the ModuleLoader path, avoiding
        the need for manual configuration.
        """
        base_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        ansible_mitogen.loaders.connection_loader.add_directory(
            os.path.join(base_dir, 'connection')
        )
        ansible_mitogen.loaders.action_loader.add_directory(
            os.path.join(base_dir, 'action')
        )

    def _install_wrappers(self):
        """
        Install our PluginLoader monkey patches and update global variables
        with references to the real functions.
        """
        ansible_mitogen.loaders.action_loader.get_with_context = wrap_action_loader__get_with_context
        ansible_mitogen.loaders.connection_loader.get_with_context = wrap_connection_loader__get_with_context

        global worker__run
        worker__run = ansible.executor.process.worker.WorkerProcess.run
        ansible.executor.process.worker.WorkerProcess.run = wrap_worker__run

    def _remove_wrappers(self):
        """
        Uninstall the PluginLoader monkey patches.
        """
        ansible_mitogen.loaders.action_loader.get_with_context = (
            ansible_mitogen.loaders.action_loader__get_with_context
        )
        ansible_mitogen.loaders.connection_loader.get_with_context = (
            ansible_mitogen.loaders.connection_loader__get_with_context
        )
        ansible.executor.process.worker.WorkerProcess.run = worker__run

    def install(self):
        self._add_plugin_paths()
        self._install_wrappers()

    def remove(self):
        self._remove_wrappers()


class StrategyMixin(object):
    """
    This mix-in enhances any built-in strategy by arranging for an appropriate
    WorkerModel instance to be constructed as necessary, or for the existing
    one to be reused.

    The WorkerModel in turn arranges for a connection multiplexer to be started
    somewhere (by default in an external process), and for WorkerProcesses to
    grow support for using those top-level services to communicate with remote
    hosts.

    Mitogen:

        A private Broker IO multiplexer thread is created to dispatch IO
        between the local Router and any connected streams, including streams
        connected to Ansible WorkerProcesses, and SSH commands implementing
        connections to remote machines.

        A Router is created that implements message dispatch to any locally
        registered handlers, and message routing for remote streams. Router is
        the junction point through which WorkerProceses and remote SSH contexts
        can communicate.

        Router additionally adds message handlers for a variety of base
        services, review the Standard Handles section of the How It Works guide
        in the documentation.

        A ContextService is installed as a message handler in the connection
        mutliplexer subprocess and run on a private thread. It is responsible
        for accepting requests to establish new SSH connections from worker
        processes, and ensuring precisely one connection exists and is reused
        for subsequent playbook steps. The service presently runs in a single
        thread, so to begin with, new SSH connections are serialized.

        Finally a mitogen.unix listener is created through which WorkerProcess
        can establish a connection back into the connection multiplexer, in
        order to avail of ContextService. A UNIX listener socket is necessary
        as there is no more sane mechanism to arrange for IPC between the
        Router in the connection multiplexer, and the corresponding Router in
        the worker process.

    Ansible:

        PluginLoader monkey patches are installed to catch attempts to create
        connection and action plug-ins.

        For connection plug-ins, if the desired method is "local" or "ssh", it
        is redirected to one of the "mitogen_*" connection plug-ins. That
        plug-in implements communication via a UNIX socket connection to the
        connection multiplexer process, and uses ContextService running there
        to establish a persistent connection to the target.

        For action plug-ins, the original class is looked up as usual, but a
        new subclass is created dynamically in order to mix-in
        ansible_mitogen.target.ActionModuleMixin, which overrides many of the
        methods usually inherited from ActionBase in order to replace them with
        pure-Python equivalents that avoid the use of shell.

        In particular, _execute_module() is overridden with an implementation
        that uses ansible_mitogen.target.run_module() executed in the target
        Context. run_module() implements module execution by importing the
        module as if it were a normal Python module, and capturing its output
        in the remote process. Since the Mitogen module loader is active in the
        remote process, all the heavy lifting of transferring the action module
        and its dependencies are automatically handled by Mitogen.
    """

    def _queue_task(self, host, task, task_vars, play_context):
        """
        Many PluginLoader caches are defective as they are only populated in
        the ephemeral WorkerProcess. Touch each plug-in path before forking to
        ensure all workers receive a hot cache.
        """
        ansible_mitogen.loaders.module_loader.find_plugin(
            name=task.action,
            mod_type='',
        )
        ansible_mitogen.loaders.action_loader.get(
            name=task.action,
            class_only=True,
        )
        if play_context.connection is not ansible.utils.sentinel.Sentinel:
            # 2.8 appears to defer computing this until inside the worker.
            # TODO: figure out where it has moved.
            ansible_mitogen.loaders.connection_loader.get(
                name=play_context.connection,
                class_only=True,
            )

        return super(StrategyMixin, self)._queue_task(
            host=host,
            task=task,
            task_vars=task_vars,
            play_context=play_context,
        )

    def _get_worker_model(self):
        """
        In classic mode a single :class:`WorkerModel` exists, which manages
        references and configuration of the associated connection multiplexer
        process.
        """
        return ansible_mitogen.process.get_classic_worker_model()

    def run(self, iterator, play_context, result=0):
        """
        Wrap :meth:`run` to ensure requisite infrastructure and modifications
        are configured for the duration of the call.
        """
        wrappers = AnsibleWrappers()
        self._worker_model = self._get_worker_model()
        ansible_mitogen.process.set_worker_model(self._worker_model)
        try:
            self._worker_model.on_strategy_start()
            try:
                wrappers.install()
                try:
                    run = super(StrategyMixin, self).run
                    return mitogen.core._profile_hook('Strategy',
                        lambda: run(iterator, play_context)
                    )
                finally:
                    wrappers.remove()
            finally:
                self._worker_model.on_strategy_complete()
        finally:
            ansible_mitogen.process.set_worker_model(None)

    def _smuggle_to_connection_reset(self, task, play_context, iterator, target_host):
        """
        Create a templar and make it available for use in Connection.reset().
        This allows templated connection variables to be used when Mitogen
        reconstructs its connection stack.
        """
        variables = self._variable_manager.get_vars(
            play=iterator._play, host=target_host, task=task,
            _hosts=self._hosts_cache, _hosts_all=self._hosts_cache_all,
        )
        templar = ansible.template.Templar(
            loader=self._loader, variables=variables,
        )

        # Required for remote_user option set by variable (e.g. ansible_user).
        # Without it remote_user in ansible.cfg gets used.
        play_context = play_context.set_task_and_variable_override(
            task=task, variables=variables, templar=templar,
        )
        play_context.post_validate(templar=templar)

        # Required for timeout option set by variable (e.g. ansible_timeout).
        # Without it the task timeout keyword (default: 0) gets used.
        play_context.update_vars(variables)

        # Stash the task and templar somewhere Connection.reset() can find it
        play_context.vars.update({
            '_mitogen.smuggled.reset_connection': (task, templar),
        })
        return play_context

    def _execute_meta(self, task, play_context, iterator, target_host):
        if task.args['_raw_params'] == 'reset_connection':
            play_context = self._smuggle_to_connection_reset(
                task, play_context, iterator, target_host,
            )

        return super(StrategyMixin, self)._execute_meta(
            task, play_context, iterator, target_host,
        )
