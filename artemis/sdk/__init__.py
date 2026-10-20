# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ARTEMIS SDK for running mobile automation tasks.

This package provides APIs for interacting with mobile devices and executing
tasks.
"""

from artemis.sdk import builders, types
from artemis.sdk.agent import Agent


def __getattr__(name):
    # Keep the one-shot SDK and stdio server's import path unchanged. Persistent
    # Session infrastructure is loaded only by callers using this new API.
    if name in ("RoleSession", "RoleSessionOpenError", "RoleTaskResult", "TaskHandle"):
        from artemis.sdk import role_session

        value = getattr(role_session, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Agent", "RoleSession", "RoleSessionOpenError", "RoleTaskResult", "TaskHandle"]
__all__ += types.__all__
__all__ += builders.__all__
