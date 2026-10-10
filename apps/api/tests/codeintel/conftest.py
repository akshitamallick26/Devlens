# Fixtures reused from other suites: the git fixture repository (real
# ingestion/parsing) and the overview tests' row builder (exact graph shapes).
from tests.automation.conftest import automation_fixture_repo  # noqa: F401
from tests.overview.test_service_db import builder  # noqa: F401
