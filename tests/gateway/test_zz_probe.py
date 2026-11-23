from gateway import owner_delivery_policy as m


def test_probe_classify(monkeypatch):
    real = m.filter_artifacts
    calls = []

    def spy(paths, extensions=None):
        out = real(paths, extensions)
        calls.append((list(paths), extensions, out))
        return out

    monkeypatch.setattr(m, "filter_artifacts", spy)
    print(m.classify_kanban_event(
        kind="completed", payload={"artifacts": ["/tmp/report.pdf"], "summary": "x"},
        has_open_ancestor=False, extensions=None))
    print("calls:", calls)
