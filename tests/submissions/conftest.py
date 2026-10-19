"""A loopback fake collector for the submission tests."""

import pytest
from fake_collector import FakeCollector


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()
