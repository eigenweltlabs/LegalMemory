"""Marks the suite as a package so ``tests.conftest`` is importable.

Thirteen call sites import shared constants from the conftest by name —
``from tests.conftest import TEST_DATABASE_URL`` and friends. That only resolves when the
repository root is on ``sys.path``, and whether it is depends entirely on how pytest was
started: ``python -m pytest`` puts the working directory there, the ``pytest`` console
script does not. The suite therefore passed locally and died in CI with
``ModuleNotFoundError: No module named 'tests'`` — five modules failing at collection in
the fast job, all eleven round-trip tests erroring in the backup job.

With this file present pytest's default prepend import mode walks up past ``tests/`` and
puts the repository root on ``sys.path`` instead, under either invocation, and loads the
conftest as ``tests.conftest`` — the same module object those imports then get, rather
than a second copy of it.

The cost is that ``tests/`` itself is no longer on ``sys.path``, so sibling helpers are
imported package-qualified too: ``from tests.connector_replay import build``, never
``from connector_replay import build``.
"""
