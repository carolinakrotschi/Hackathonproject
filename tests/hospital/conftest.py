import json

import pytest
from hospital.state import Hospital

from klinikum import DEMO, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hospital(clock: FakeClock) -> Hospital:
    return Hospital.load(DEMO, clock=clock)


@pytest.fixture(scope="session")
def scenario() -> dict:
    return json.loads((DEMO / "scenario.json").read_text(encoding="utf-8"))


@pytest.fixture
def client(hospital):
    hospital_api = pytest.importorskip("app.hospital_api")
    server = pytest.importorskip("app.server")
    from fastapi.testclient import TestClient

    hospital_api.set_hospital(hospital)
    try:
        with TestClient(server.app) as test_client:
            yield test_client
    finally:
        hospital_api.stop_sim()
