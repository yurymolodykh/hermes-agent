import asyncio, os, sys, time, tempfile
from pathlib import Path

REPO = "/home/hermes/.hermes/hermes-agent/.worktrees/t_6c16a6cb"
sys.path.insert(0, REPO)
home = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ["HERMES_STATE_DB_GUARD_BYPASS"] = "1"
from hermes_cli.web_routers.handler_offload import HANDLER_OFFLOAD, off_loop
from hermes_cli.web_routers.files import _fs_write_text_blocking


async def main():
    HANDLER_OFFLOAD.reset()
    HANDLER_OFFLOAD.resolve()
    HANDLER_OFFLOAD._enabled = False
    HANDLER_OFFLOAD._settled = True
    print("mode", HANDLER_OFFLOAD.snapshot()["mode"])
    text = "x" * (8 * 1024 * 1024)
    samples = []
    rec = [False]

    async def mon():
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            samples.append(((time.perf_counter() - t0 - 0.01) * 1000, rec[0]))

    m = asyncio.create_task(mon())
    await asyncio.sleep(0.05)
    rec[0] = True
    t0 = time.perf_counter()
    for i in range(8):
        a = time.perf_counter()
        await off_loop(_fs_write_text_blocking, home / f"w{i}.txt", text)
        print("write", i, round((time.perf_counter() - a) * 1000, 1))
    print("total", round((time.perf_counter() - t0) * 1000, 1))
    rec[0] = False
    await asyncio.sleep(0.05)
    m.cancel()
    print("samples recorded:", [(round(v, 1), r) for v, r in samples if r])

asyncio.run(main())
