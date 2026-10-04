"""Keep the pure avatar contract tests independent from the shared test database."""

import pytest


@pytest.fixture(scope="module", autouse=True)
def temp_test_db():
    """Override the repository fixture that creates and drops a temporary DB."""
    yield


@pytest.fixture(scope="module", autouse=True)
def sync_test_embedding_dimension_from_db():
    """Disable the parent autouse fixture that reaches the DB through db_pool."""
    yield


@pytest.fixture(scope="module", autouse=True)
def configure_agent_for_tests():
    """Disable the parent autouse fixture that mutates test agent configuration."""
    yield


@pytest.fixture(scope="module", autouse=True)
def apply_repo_migrations():
    """Disable migration setup for tests that deliberately use no database."""
    yield


@pytest.fixture(autouse=True)
def setup_db():
    """Disable per-test setup that otherwise acquires the database pool."""
    yield
