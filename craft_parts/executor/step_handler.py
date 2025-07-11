# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2016-2025 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Handle the execution of built-in or user specified step commands."""

import dataclasses
import functools
import json
import logging
import os
import selectors
import socket
import tempfile
from pathlib import Path
from typing import TextIO

from craft_parts import errors, packages
from craft_parts.infos import StepInfo
from craft_parts.parts import Part
from craft_parts.plugins import Plugin
from craft_parts.sources.local_source import SourceHandler
from craft_parts.steps import Step
from craft_parts.utils import process
from craft_parts.utils.partition_utils import DEFAULT_PARTITION

from . import filesets
from .filesets import Fileset
from .migration import migrate_files

logger = logging.getLogger(__name__)

Stream = TextIO | int | None


@dataclasses.dataclass(frozen=True)
class StepPartitionContents:
    """Files and directories to be added to the step's state."""

    files: set[str] = dataclasses.field(default_factory=set)
    dirs: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass(frozen=True)
class StagePartitionContents(StepPartitionContents):
    """Files and directories for both stage and backstage in the step's state."""

    backstage_files: set[str] = dataclasses.field(default_factory=set)
    backstage_dirs: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass(init=False)
class StepContents:
    """Contents mapped to partitions."""

    partitions_contents: dict[str, StepPartitionContents | StagePartitionContents] = (
        dataclasses.field(default_factory=dict)
    )

    def __init__(
        self, *, partitions: list[str] | None = None, stage: bool = False
    ) -> None:
        if partitions is None or len(partitions) == 0:
            partitions = [DEFAULT_PARTITION]
        if stage:
            self.partitions_contents = {
                partition: StagePartitionContents() for partition in partitions
            }
            return
        self.partitions_contents = {
            partition: StepPartitionContents() for partition in partitions
        }


class StepHandler:
    """Executes built-in or user-specified step commands.

    The step handler takes care of the execution of a step, using either
    a built-in set of actions to be taken, or executing a user-defined
    script defined in the part specification User-defined scripts may also
    call the built-in handler for a step by invoking a control utility.
    This class implements the built-in handlers and a FIFO-based mechanism
    and API to be used by the external control utility to communicate with
    the running instance.
    """

    def __init__(
        self,
        part: Part,
        *,
        step_info: StepInfo,
        plugin: Plugin,
        source_handler: SourceHandler | None,
        env: str,
        stdout: Stream = None,
        stderr: Stream = None,
        partitions: list[str] | None = None,
    ) -> None:
        self._part = part
        self._step_info = step_info
        self._plugin = plugin
        self._source_handler = source_handler
        self._env = env
        self._stdout = stdout
        self._stderr = stderr
        self._partitions = partitions

    def run_builtin(self) -> StepContents:
        """Run the built-in commands for the current step."""
        step = self._step_info.step

        if step == Step.PULL:
            handler = self._builtin_pull
        elif step == Step.OVERLAY:
            handler = self._builtin_overlay
        elif step == Step.BUILD:
            handler = self._builtin_build
        elif step == Step.STAGE:
            handler = self._builtin_stage
        elif step == Step.PRIME:
            handler = self._builtin_prime
        else:
            raise RuntimeError(
                "Request to run the built-in handler for an invalid step."
            )

        return handler()

    def _builtin_pull(self) -> StepContents:
        if self._source_handler:
            self._source_handler.pull()

        pull_commands = self._plugin.get_pull_commands()

        if pull_commands:
            try:
                _create_and_run_script(
                    pull_commands,
                    script_path=self._part.part_run_dir.absolute() / "pull.sh",
                    cwd=self._part.part_src_subdir,
                    stdout=self._stdout,
                    stderr=self._stderr,
                )
            except process.ProcessError as process_error:
                raise (
                    errors.PluginPullError(part_name=self._part.name)
                ) from process_error

        return StepContents()

    @staticmethod
    def _builtin_overlay() -> StepContents:
        return StepContents()

    def _builtin_build(self) -> StepContents:
        # Plugin commands.
        build_commands = self._plugin.get_build_commands()

        # save script to set the build environment
        build_environment_script_path = (
            self._part.part_run_dir.absolute() / "environment.sh"
        )
        build_environment_script_path.write_text(self._env)
        build_environment_script_path.chmod(0o644)

        try:
            _create_and_run_script(
                build_commands,
                script_path=self._part.part_run_dir.absolute() / "build.sh",
                environment_script_path=build_environment_script_path,
                cwd=self._part.part_build_subdir,
                stdout=self._stdout,
                stderr=self._stderr,
            )
        except process.ProcessError as process_error:
            raise errors.PluginBuildError(
                part_name=self._part.name,
                plugin_name=self._part.plugin_name,
                stderr=process_error.result.stderr,
            ) from process_error

        return StepContents()

    def _builtin_stage(self) -> StepContents:
        stage_fileset = Fileset(
            self._part.spec.stage_files,
            name="stage",
            default_partition=self._step_info.default_partition,
        )

        def pkgconfig_fixup(file_path: str) -> None:
            if os.path.islink(file_path):
                return
            if not file_path.endswith(".pc"):
                return
            packages.fix_pkg_config(
                prefix_prepend=self._part.stage_dir,
                pkg_config_file=Path(file_path),
                prefix_trim=self._part.part_install_dir,
            )

        step_contents = StepContents(stage=True)

        if self._partitions:
            backstage_files, backstage_dirs = filesets.migratable_filesets(
                Fileset(
                    [f"({self._step_info.default_partition})/*"],
                    name="backstage",
                    default_partition=self._step_info.default_partition,
                ),
                str(self._part.part_export_dir),
                self._step_info.default_partition,
                self._step_info.default_partition,
            )
            for partition in self._partitions:
                partition_files, partition_dirs = filesets.migratable_filesets(
                    stage_fileset,
                    str(self._part.part_install_dirs[partition]),
                    self._step_info.default_partition,
                    partition,
                )
                partition_files, partition_dirs = migrate_files(
                    files=partition_files,
                    dirs=partition_dirs,
                    srcdir=self._part.part_install_dirs[partition],
                    destdir=self._part.dirs.get_stage_dir(partition),
                    fixup_func=pkgconfig_fixup,
                )
                # Backstage content is managed only in the default partition
                if partition == self._step_info.default_partition:
                    backstage_files, backstage_dirs = migrate_files(
                        files=backstage_files,
                        dirs=backstage_dirs,
                        srcdir=self._part.part_export_dir,
                        destdir=self._part.backstage_dir,
                    )
                    step_contents.partitions_contents[partition] = (
                        StagePartitionContents(
                            files=partition_files,
                            dirs=partition_dirs,
                            backstage_files=backstage_files,
                            backstage_dirs=backstage_dirs,
                        )
                    )
                else:
                    step_contents.partitions_contents[partition] = (
                        StagePartitionContents(
                            files=partition_files, dirs=partition_dirs
                        )
                    )
        else:
            files, dirs = filesets.migratable_filesets(
                stage_fileset,
                str(self._part.part_install_dir),
                DEFAULT_PARTITION,
            )
            files, dirs = migrate_files(
                files=files,
                dirs=dirs,
                srcdir=self._part.part_install_dir,
                destdir=self._part.stage_dir,
                fixup_func=pkgconfig_fixup,
            )
            backstage_files, backstage_dirs = filesets.migratable_filesets(
                Fileset(["*"], name="backstage"),
                str(self._part.part_export_dir),
                DEFAULT_PARTITION,
            )
            backstage_files, backstage_dirs = migrate_files(
                files=backstage_files,
                dirs=backstage_dirs,
                srcdir=self._part.part_export_dir,
                destdir=self._part.backstage_dir,
            )
            step_contents.partitions_contents[DEFAULT_PARTITION] = (
                StagePartitionContents(
                    files=files,
                    dirs=dirs,
                    backstage_files=backstage_files,
                    backstage_dirs=backstage_dirs,
                )
            )

        return step_contents

    def _builtin_prime(self) -> StepContents:
        prime_fileset = Fileset(
            self._part.spec.prime_files,
            name="prime",
            default_partition=self._step_info.default_partition,
        )

        # If we're priming and we don't have an explicit set of files to prime
        # include the files from the stage step
        if prime_fileset.entries == ["*"] or len(prime_fileset.includes) == 0:
            stage_fileset = Fileset(
                self._part.spec.stage_files,
                name="stage",
                default_partition=self._step_info.default_partition,
            )
            prime_fileset.combine(stage_fileset)

        step_contents = StepContents()

        if self._partitions:
            for partition in self._partitions:
                partition_files, partition_dirs = filesets.migratable_filesets(
                    prime_fileset,
                    str(self._part.part_install_dirs[partition]),
                    self._step_info.default_partition,
                    partition,
                )

                srcdir = self._part.dirs.get_stage_dir(partition)
                destdir = self._part.dirs.get_prime_dir(partition)

                partition_files, partition_dirs = migrate_files(
                    files=partition_files,
                    dirs=partition_dirs,
                    srcdir=srcdir,
                    destdir=destdir,
                    permissions=self._part.spec.permissions,
                )

                step_contents.partitions_contents[partition] = StepPartitionContents(
                    files=partition_files, dirs=partition_dirs
                )

        else:
            files, dirs = filesets.migratable_filesets(
                prime_fileset,
                str(self._part.part_install_dir),
                DEFAULT_PARTITION,
            )
            files, dirs = migrate_files(
                files=files,
                dirs=dirs,
                srcdir=self._part.stage_dir,
                destdir=self._part.prime_dir,
                permissions=self._part.spec.permissions,
            )
            step_contents.partitions_contents[DEFAULT_PARTITION] = (
                StepPartitionContents(files=files, dirs=dirs)
            )

        return step_contents

    def run_scriptlet(
        self,
        scriptlet: str,
        *,
        scriptlet_name: str,
        step: Step,
        work_dir: Path,
    ) -> None:
        """Execute a scriptlet.

        :param scriptlet: the scriptlet to run.
        :param work_dir: the directory where the script will be executed.
        """
        with tempfile.TemporaryDirectory() as tempdir:
            ctl_socket_path = os.path.join(tempdir, "craftctl.socket")
            ctl_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ctl_socket.bind(ctl_socket_path)
            ctl_socket.listen(1)

            selector = self._ctl_server_selector(step, scriptlet_name, ctl_socket)

            environment = f"export PARTS_CTL_SOCKET={ctl_socket_path}\n" + self._env
            environment_script_path = Path(tempdir) / "scriptlet_environment.sh"
            environment_script_path.write_text(environment)
            environment_script_path.chmod(0o644)

            try:
                _create_and_run_script(
                    [scriptlet],
                    script_path=Path(tempdir) / "scriptlet.sh",
                    cwd=work_dir,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    environment_script_path=environment_script_path,
                    selector=selector,
                )
            except process.ProcessError as process_error:
                raise errors.ScriptletRunError(
                    part_name=self._part.name,
                    scriptlet_name=scriptlet_name,
                    exit_code=process_error.result.returncode,
                    stderr=process_error.result.stderr,
                ) from process_error
            finally:
                ctl_socket.close()

    def _ctl_server_selector(
        self, step: Step, scriptlet_name: str, stream: socket.socket
    ) -> selectors.BaseSelector:
        selector = selectors.SelectSelector()

        def accept(sock: socket.socket, _mask: int) -> None:
            conn, addr = sock.accept()
            selector.register(conn, selectors.EVENT_READ, read)

        def read(conn: socket.socket, _mask: int) -> None:
            data = conn.recv(1024)
            logger.debug(f"ctl server received: {data!s}")
            if not data:
                selector.unregister(conn)
                conn.close()
                return

            try:
                retval = self._handle_control_api(
                    step, scriptlet_name, data.decode("utf-8")
                )
                conn.send((f"OK {retval!s}\n" if retval else "OK\n").encode())
            except errors.PluginBuildError:
                # If craftctl default raises PluginBuildError, pass it upwards.
                raise
            except errors.PartsError as error:
                conn.send(f"ERR {error!s}\n".encode())

        selector.register(stream, selectors.EVENT_READ, accept)

        return selector

    def _handle_control_api(
        self, step: Step, scriptlet_name: str, function_call: str
    ) -> str:
        """Parse the command message received from the client."""
        try:
            function_json = json.loads(function_call)
        except json.decoder.JSONDecodeError as err:
            raise RuntimeError(
                f"{scriptlet_name!r} scriptlet called a function with invalid json: "
                f"{function_call}"
            ) from err

        for attr in ["function", "args"]:
            if attr not in function_json:
                raise RuntimeError(
                    f"{scriptlet_name!r} control call missing attribute {attr!r}"
                )

        cmd_name = function_json["function"]
        cmd_args = function_json["args"]

        return self._process_api_commands(
            cmd_name, cmd_args, step=step, scriptlet_name=scriptlet_name
        )

    def _process_api_commands(
        self, cmd_name: str, cmd_args: list[str], *, step: Step, scriptlet_name: str
    ) -> str:
        """Invoke API command actions."""
        retval = ""

        invalid_control_api_call = functools.partial(
            errors.InvalidControlAPICall,
            part_name=self._part.name,
            scriptlet_name=scriptlet_name,
        )

        if cmd_name == "default":
            if len(cmd_args) > 0:
                raise invalid_control_api_call(
                    message=f"invalid arguments to command {cmd_name!r}",
                )
            self._execute_builtin_handler(step)
        elif cmd_name == "set":
            if len(cmd_args) != 1:
                raise invalid_control_api_call(
                    message=(f"invalid arguments to command {cmd_name!r}"),
                )

            if "=" not in cmd_args[0]:
                raise invalid_control_api_call(
                    message=(
                        f"invalid arguments to command {cmd_name!r} (want key=value)"
                    ),
                )

            name, value = cmd_args[0].split("=")

            try:
                self._step_info.set_project_var(name, value)
            except (ValueError, RuntimeError) as err:
                raise errors.InvalidControlAPICall(
                    part_name=self._part.name,
                    scriptlet_name=scriptlet_name,
                    message=str(err),
                ) from err
        elif cmd_name == "get":
            if len(cmd_args) != 1:
                raise invalid_control_api_call(
                    message=(f"invalid number of arguments to command {cmd_name!r}"),
                )
            (name,) = cmd_args

            try:
                retval = self._step_info.get_project_var(name, raw_read=True)
            except ValueError as err:
                raise errors.InvalidControlAPICall(
                    part_name=self._part.name,
                    scriptlet_name=scriptlet_name,
                    message=str(err),
                ) from err
        else:
            raise invalid_control_api_call(
                message=f"invalid command {cmd_name!r}",
            )

        return retval

    def _execute_builtin_handler(self, step: Step) -> None:
        if step == Step.PULL:
            self._builtin_pull()
        elif step == Step.OVERLAY:
            self._builtin_overlay()
        elif step == Step.BUILD:
            self._builtin_build()
        elif step == Step.STAGE:
            self._builtin_stage()
        elif step == Step.PRIME:
            self._builtin_prime()


def _create_and_run_script(
    commands: list[str],
    script_path: Path,
    cwd: Path,
    stdout: Stream,
    stderr: Stream,
    environment_script_path: Path | None = None,
    selector: selectors.BaseSelector | None = None,
) -> None:
    """Create a script with step-specific commands and execute it."""
    with script_path.open("w") as run_file:
        print("#!/bin/bash", file=run_file)
        print("set -euo pipefail", file=run_file)

        if environment_script_path:
            print(f"source {environment_script_path}", file=run_file)

        print("set -x", file=run_file)

        for cmd in commands:
            print(cmd, file=run_file)

    script_path.chmod(0o755)
    logger.debug("Executing %r", script_path)

    process.run(
        [script_path],
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        check=True,
        selector=selector,
    )
