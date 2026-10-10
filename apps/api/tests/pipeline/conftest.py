# The git fixture repository lives in tests/automation/conftest.py, which
# pytest only exposes to tests under tests/automation.
from tests.automation.conftest import automation_fixture_repo  # noqa: F401
