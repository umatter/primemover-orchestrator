"""Repository-wide test setup, for both test paths (orchestrator/tests, pmspec/tests).

Git runs hooks with its own location variables in the environment: a pre-push
from a worktree exports ``GIT_DIR`` pointing at that worktree's git directory.
Tests that create a scratch repository and run git in it (pmspec's lock
provenance, the MCP compile tool) then act on the hook's repository instead of
their own -- six of them failed under the ``make hooks`` pre-push from a
worktree while passing on every direct run. Clearing those variables for each
test keeps every git call on the repository the test made.
"""

import pytest

_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
)


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GIT_LOCATION_VARS:
        monkeypatch.delenv(name, raising=False)
