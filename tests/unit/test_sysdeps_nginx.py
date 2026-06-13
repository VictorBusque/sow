"""Tests for the NGINX wrappers.

DoD core: ``test`` builds the exact argv with and without ``-p``; ``reload``
issues ``systemctl --user reload outpost-nginx`` (this test guards the
``prd.md`` §169 decision — if someone later "simplifies" to ``nginx -s reload``,
it fails); ``test`` propagates ``nginx -t`` syntax errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outpost.sysdeps import nginx
from outpost.sysdeps.run import CompletedProcess, SubprocessError
from tests.mocks import FakeRunner

CONF = Path("/srv/outpost/generated/nginx/staging/nginx.conf")
PREFIX = Path("/srv/outpost/generated/nginx/staging")


# ---------------------------------------------------------------------------
# test (with / without -p prefix)
# ---------------------------------------------------------------------------


def test_test_argv_without_prefix():
    fake = FakeRunner()
    fake.script(["nginx", "-t", "-c", str(CONF)], returns=CompletedProcess(0, "syntax is ok\n", ""))
    nginx.test(fake, CONF)
    assert fake.argvs == [["nginx", "-t", "-c", str(CONF)]]


def test_test_argv_with_prefix():
    fake = FakeRunner()
    fake.script(
        ["nginx", "-p", str(PREFIX), "-t", "-c", str(CONF)],
        returns=CompletedProcess(0, "syntax is ok\n", ""),
    )
    nginx.test(fake, CONF, prefix=PREFIX)
    assert fake.argvs == [["nginx", "-p", str(PREFIX), "-t", "-c", str(CONF)]]


def test_test_propagates_stderr_on_syntax_error():
    fake = FakeRunner()
    fake.script(
        ["nginx", "-t", "-c", str(CONF)],
        returns=CompletedProcess(1, "", 'nginx: [emerg] unknown directive "foo"'),
    )
    with pytest.raises(SubprocessError, match=r"\[emerg\] unknown directive"):
        nginx.test(fake, CONF)


# ---------------------------------------------------------------------------
# reload (prd.md §169: systemctl --user reload, never nginx -s reload)
# ---------------------------------------------------------------------------


def test_reload_uses_systemctl_user_reload_outpost_nginx():
    fake = FakeRunner()
    fake.script(
        ["systemctl", "--user", "reload", "outpost-nginx"], returns=CompletedProcess(0, "", "")
    )
    nginx.reload(fake)
    assert fake.argvs == [["systemctl", "--user", "reload", "outpost-nginx"]]


def test_reload_unit_name_constant():
    assert nginx.NGINX_UNIT == "outpost-nginx"
