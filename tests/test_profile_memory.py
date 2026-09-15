import importlib.util


def test_profile_memory_runtime_service_is_removed():
    assert importlib.util.find_spec("backend.services.profile_memory") is None
