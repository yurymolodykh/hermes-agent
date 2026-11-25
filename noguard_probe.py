def pytest_configure(config):
    import tests.conftest as cf
    cf._REAL_HERMES_ROOT_CANDIDATES.clear()
