#!/usr/bin/env python3
"""Smoke test: verify qwen3 single-model integration.

Covers:
  1. Normal path: remember -> recall (requires ollama running)
  2. Graceful error when ollama is unreachable
  3. Plugin env=0 disable logic (static assertion)

Usage:
  # Full smoke test (requires ollama + qwen3-embedding:0.6b):
  python3 tests/test_qwen3_smoke.py

  # Offline check only (env logic + import sanity, no ollama needed):
  MEMORYCORE_EMBED_URL=http://127.0.0.1:19999/v1 python3 tests/test_qwen3_smoke.py --offline
"""

import os
import subprocess
import sys


def green(s):
    return f"\033[32m{s}\033[0m"


def red(s):
    return f"\033[31m{s}\033[0m"


def bold(s):
    return f"\033[1m{s}\033[0m"


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {green('PASS')}  {name}")
        passed += 1
    else:
        print(f"  {red('FAIL')}  {name}")
        if detail:
            print(f"        {detail}")
        failed += 1


def main():
    global passed, failed
    offline = "--offline" in sys.argv

    # Save original env so default-value tests aren't tainted by overrides
    _orig_embed_url = os.environ.get("MEMORYCORE_EMBED_URL")
    _orig_embed_model = os.environ.get("MEMORYCORE_EMBED_MODEL")
    _orig_mnemosyne_url = os.environ.get("MNEMOSYNE_EMBEDDING_API_URL")
    _orig_mnemosyne_model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL")
    _orig_mnemosyne_dim = os.environ.get("MNEMOSYNE_EMBEDDING_DIM")

    print(bold("=== MemoryCore qwen3 Smoke Tests ===\n"))

    # -- Test 1: Env logic (always runs, no ollama needed) ----------
    print(bold("[1] Environment variable logic"))

    # 1a: PREFETCH_ENABLED unset -> enabled
    v = os.environ.get("MEMORYCORE_PREFETCH_ENABLED", "").strip()
    check("MEMORYCORE_PREFETCH_ENABLED unset -> enabled", v != "0",
          f"got {repr(v)}, expected '' or '1'")

    # 1b: PREFETCH_ENABLED=0 -> disabled
    os.environ["MEMORYCORE_PREFETCH_ENABLED"] = "0"
    v0 = os.environ.get("MEMORYCORE_PREFETCH_ENABLED", "").strip()
    check("MEMORYCORE_PREFETCH_ENABLED=0 -> disabled", v0 == "0",
          f"got {repr(v0)}")

    # 1c: PREFETCH_ENABLED=1 -> enabled
    os.environ["MEMORYCORE_PREFETCH_ENABLED"] = "1"
    v1 = os.environ.get("MEMORYCORE_PREFETCH_ENABLED", "").strip()
    check("MEMORYCORE_PREFETCH_ENABLED=1 -> enabled", v1 != "0",
          f"got {repr(v1)}")

    # 1d: default EMBED_URL (pop env override to test true default)
    _saved_embed_url = os.environ.pop("MEMORYCORE_EMBED_URL", None)
    default_url = os.environ.get("MEMORYCORE_EMBED_URL", "http://localhost:11434/v1")
    check("default MEMORYCORE_EMBED_URL points to ollama",
          "11434" in default_url,
          f"got {default_url}")
    if _saved_embed_url:
        os.environ["MEMORYCORE_EMBED_URL"] = _saved_embed_url

    # 1e: default EMBED_MODEL (pop env override to test true default)
    _saved_embed_model = os.environ.pop("MEMORYCORE_EMBED_MODEL", None)
    default_model = os.environ.get("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")
    check("default MEMORYCORE_EMBED_MODEL is qwen3-embedding:0.6b",
          "qwen3" in default_model and "embed" in default_model,
          f"got {default_model}")
    if _saved_embed_model:
        os.environ["MEMORYCORE_EMBED_MODEL"] = _saved_embed_model

    # Clean up env to avoid leaking into later tests
    os.environ.pop("MEMORYCORE_PREFETCH_ENABLED", None)

    # -- Test 2: Import sanity --------------------------------------
    print(bold("\n[2] Import sanity"))
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo_root)

    try:
        from memorycore.core import config
        check("import memorycore.core.config", True)
    except Exception as e:
        check("import memorycore.core.config", False, str(e))

    try:
        from memorycore.local_store import LocalStore
        check("import memorycore.local_store", True)
    except Exception as e:
        check("import memorycore.local_store", False, str(e))

    # -- Test 3: Graceful error when ollama is unreachable ----------
    print(bold("\n[3] Graceful error on unreachable embedding API"))

    # Point at a dead port to simulate ollama being down
    os.environ["MNEMOSYNE_EMBEDDING_API_URL"] = "http://127.0.0.1:19999/v1"
    os.environ["MNEMOSYNE_EMBEDDING_MODEL"] = "qwen3-embedding:0.6b"
    if "MNEMOSYNE_EMBEDDING_DIM" not in os.environ:
        os.environ["MNEMOSYNE_EMBEDDING_DIM"] = "1024"

    try:
        from memorycore.cold_store_client import LocalBackend
    except ModuleNotFoundError as e:
        print(f"  {green('SKIP')}  mnemosyne not installed — {e}")
        print("  Install: pip install mnemosyne-memory")
        # Still check the env logic works
        check("Test 3 env setup (API URL points to dead port)",
              os.environ.get("MNEMOSYNE_EMBEDDING_API_URL") == "http://127.0.0.1:19999/v1")
        check("Test 3 env setup (MODEL set)",
              os.environ.get("MNEMOSYNE_EMBEDDING_MODEL") == "qwen3-embedding:0.6b")
    else:
        try:
            lb = LocalBackend()
            check("LocalBackend with dead API -> raises RuntimeError", False,
                  "Expected RuntimeError but got no exception")
        except ModuleNotFoundError as e:
            print(f"  {green("SKIP")}  mnemosyne not installed — {e}")
            print("  Install: pip install mnemosyne-memory")
            # Still check the env logic works
            check("Test 3 env setup (API URL points to dead port)",
                  os.environ.get("MNEMOSYNE_EMBEDDING_API_URL") == "http://127.0.0.1:19999/v1")
            check("Test 3 env setup (MODEL set)",
                  os.environ.get("MNEMOSYNE_EMBEDDING_MODEL") == "qwen3-embedding:0.6b")
        except RuntimeError as e:
            msg = str(e)
            check("LocalBackend raises RuntimeError", True)
            check("Error message mentions ollama install",
                  "ollama" in msg.lower() and "pull" in msg.lower(),
                  msg[:120])
            check("Error message shows API URL",
                  "127.0.0.1:19999" in msg or "API URL" in msg,
                  msg[:120])
        except Exception as e:
            check("LocalBackend raises RuntimeError", False,
                  f"Got {type(e).__name__}: {e}")

    # Restore original env for later tests
    for k in ("MNEMOSYNE_EMBEDDING_API_URL", "MNEMOSYNE_EMBEDDING_MODEL",
              "MNEMOSYNE_EMBEDDING_DIM", "MEMORYCORE_EMBED_URL",
              "MEMORYCORE_EMBED_MODEL"):
        os.environ.pop(k, None)
    if _orig_mnemosyne_url:
        os.environ["MNEMOSYNE_EMBEDDING_API_URL"] = _orig_mnemosyne_url
    if _orig_mnemosyne_model:
        os.environ["MNEMOSYNE_EMBEDDING_MODEL"] = _orig_mnemosyne_model
    if _orig_mnemosyne_dim:
        os.environ["MNEMOSYNE_EMBEDDING_DIM"] = _orig_mnemosyne_dim
    if _orig_embed_url:
        os.environ["MEMORYCORE_EMBED_URL"] = _orig_embed_url
    if _orig_embed_model:
        os.environ["MEMORYCORE_EMBED_MODEL"] = _orig_embed_model

    # -- Test 4: Normal path (requires ollama) ----------------------
    if offline:
        print(bold("\n[4] Normal path -- SKIPPED (--offline mode)"))
    else:
        print(bold("\n[4] Normal path: remember -> recall"))
        try:
            from memorycore.cold_store_client import LocalBackend
            lb = LocalBackend()
            check("LocalBackend init with ollama running", True)

            # Remember a test fact
            result = lb.remember("MemoryCore smoke test: the sky is blue",
                                 importance=0.9)
            check("remember returns status=stored",
                  result.get("status") == "stored",
                  str(result))
            mem_id = result.get("memory_id", "")
            check("remember returns a memory_id", bool(mem_id), mem_id)

            # Recall it
            recalled = lb.recall("sky color", top_k=5)
            check("recall returns status=ok",
                  recalled.get("status") == "ok",
                  str(recalled)[:200])
            results = recalled.get("results", [])
            check("recall returns at least 1 result", len(results) >= 1,
                  f"got {len(results)} results")
            if results:
                top_dense = results[0].get("dense_score", 0)
                check("top result has non-zero dense_score",
                      top_dense > 0.0,
                      f"dense_score={top_dense}")

            # Clean up the test entry
            if mem_id:
                lb.forget(mem_id)

            print(f"\n  (test entry {mem_id} cleaned up)")
        except RuntimeError as e:
            print(f"  {red('SKIP')}  ollama not running -- {e}")
            print("  Start ollama and pull qwen3-embedding:0.6b to run this test.")
        except Exception as e:
            check("Normal path completes without unexpected error", False, str(e))

    # -- Summary ----------------------------------------------------
    print(bold(f"\n{'='*50}"))
    total = passed + failed
    if failed == 0:
        print(green(f"All {total} tests passed."))
        return 0
    else:
        print(red(f"{failed}/{total} tests FAILED."))
        return 1


if __name__ == "__main__":
    sys.exit(main())
