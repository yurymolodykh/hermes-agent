"""Why does ops-card-merge say the repo is not in the registry? Re-run its resolver."""
import importlib.util
import pathlib

OPS = pathlib.Path("/home/hermes/workspaces/01-orchestrator/ops")
spec = importlib.util.spec_from_file_location("pt", OPS / "bin" / "ops_publish_target.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

registry = OPS / "git-autopush.yaml"
by_repo = mod.targets_by_repo(registry)
print("repos in registry:", len(by_repo))
repo = pathlib.Path("/home/hermes/.hermes/hermes-agent").resolve()
print("resolved repo:", repo)
print("key in map:", str(repo) in by_repo)
print("candidates:", by_repo.get(str(repo)))
