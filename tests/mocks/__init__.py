"""Test doubles for the sysdeps layer — recording fakes, not host subprocesses."""

from __future__ import annotations

from tests.mocks.runner import FakeRunner, FakeRunnerNotScripted, RecordedCall

__all__ = ["FakeRunner", "FakeRunnerNotScripted", "RecordedCall"]
