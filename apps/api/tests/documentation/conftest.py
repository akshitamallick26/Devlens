"""Shared fixtures for Documentation Intelligence DB-dependent tests.
Reuses tests/automation/conftest.py's fixture-repo builder (real git repo
with a real caller -> callee relationship, real parsing + graph
construction) rather than inventing a second one — pytest only sees
fixtures from conftest.py files in ancestor directories, so a sibling
fixture must be explicitly re-exported to be visible here.
"""

from tests.automation.conftest import automation_fixture_repo  # noqa: F401

__all__ = ["automation_fixture_repo"]
