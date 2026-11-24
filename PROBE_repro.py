"""Проба: воспроизведение дефекта cache_path с '/' в prefix (до/после фикса)."""
import os
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="probe-home-"))
os.environ["HERMES_HOME"] = str(home)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import provider_media  # noqa: E402

PREFIXES = [
    "openai_flux-2-dev",                                   # безопасный, контроль
    "openai_community/scriptsnsenses-sys/flux-2-dev-free", # боевой слэшевый id
    "openai_model with space",                             # пробел
    "openai_..%2f..%2fescape",                             # попытка traversal
    "openai_..\\..\\win",                                  # windows-style
    "../escape",                                           # чистый traversal
]

base = (home / "cache" / "generated" / "images").resolve()
print("cache base:", base)
for p in PREFIXES:
    try:
        path = provider_media.cache_path("generated/images", p, "png")
        inside = base in path.resolve().parents or path.resolve().parent == base
        print(f"\nprefix={p!r}")
        print(f"  -> {path}")
        print(f"  inside cache dir (before write): {inside}")
        try:
            path.write_bytes(b"png-bytes")
            print(f"  WRITE OK, exists={path.exists()}")
        except Exception as exc:
            print(f"  WRITE FAILED: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"\nprefix={p!r}: cache_path raised {type(exc).__name__}: {exc}")

print("\n--- files under cache base:")
for f in sorted(base.rglob("*")):
    print(" ", f.relative_to(base))
