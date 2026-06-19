"""sow — a deterministic deployment engine for git-sourced systemd user services."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The version is derived from git tags at build time (hatch-vcs); read it
    # back from installed metadata rather than maintaining a hardcoded literal.
    __version__ = version("sow-cli")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"
