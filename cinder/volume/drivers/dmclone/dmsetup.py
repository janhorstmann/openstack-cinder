# SPDX-License-Identifier: Apache-2.0

#  Copyright 2024 OSISM GmbH
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from os_brick import executor
from oslo_concurrency import processutils as putils
from oslo_log import log as logging


LOG = logging.getLogger(__name__)


class DMSetup(executor.Executor):

    def _init__(self, root_helper, execute=putils.execute):
        super(DMSetup, self).__init__(root_helper, execute=execute)

    def _run(self, arg_list: list[str]):
        cmd = ["dmsetup"] + arg_list
        try:
            return self._execute(*cmd, root_helper=self._root_helper, run_as_root=True)
        except putils.ProcessExecutionError as err:
            LOG.exception("Error executing command")
            LOG.error("Cmd     :%s", err.cmd)
            LOG.error("StdOut  :%s", err.stdout)
            LOG.error("StdErr  :%s", err.stderr)
            raise

    def create(self, target: str, table: str):
        cmd = ["create", target, "--table", table]
        self._run(cmd)

    def remove(self, target: str):
        cmd = ["remove", target]
        self._run(cmd)

    def suspend(self, target: str):
        cmd = ["suspend", target]
        self._run(cmd)

    def resume(self, target: str):
        cmd = ["resume", target]
        self._run(cmd)

    def load(self, target: str, table: str):
        cmd = ["load", target, "--table", table]
        self._run(cmd)

    def message(self, target: str, sector: str, message: str):
        cmd = ["message", target, sector, message]
        self._run(cmd)

    def table(self, target: str) -> list[str]:
        cmd = ["table"]
        cmd.append(target)
        (out, err) = self._run(cmd)
        return out.strip().split(" ")

    def status(self, target: str) -> list[str]:
        cmd = ["status"]
        cmd.append(target)
        (out, err) = self._run(cmd)
        return out.strip().split(" ")

    def rename(self, target: str, name: str):
        cmd = ["rename", target, name]
        self._run(cmd)
