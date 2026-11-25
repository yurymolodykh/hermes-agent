import sys
import traceback


def pytest_configure(config):
    import tests.home_io_guard as g

    def refuse(value):
        sys.stderr.write(f"\nFIRST-REFUSE {value}\n")
        traceback.print_stack(file=sys.stderr)
        sys.stderr.write("END-FIRST-REFUSE\n")
        sys.stderr.flush()
        return None

    g.HomeIOGuard.refuse = staticmethod(refuse)
    print("TRACE-ON", file=sys.stderr)
