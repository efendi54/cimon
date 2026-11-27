# ruff: noqa: CPY001
# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "commitizen",
#     "git-changelog",
# ]
# ///
"""Bump the version and update the changelog and lockfile."""

import subprocess

# These developer tools are intentionally resolved from PATH.
subprocess.run(["cz", "bump"], check=True)  # noqa: S607
subprocess.run(["git-changelog"], check=True)  # noqa: S607
subprocess.run(["uv", "lock"], check=True)  # noqa: S607
subprocess.run(["git", "add", "CHANGELOG.md", "uv.lock"], check=True)  # noqa: S607
subprocess.run(["git", "commit", "--amend", "--no-edit"], check=True)  # noqa: S607

output = subprocess.run(
    ["cz", "version", "--project"],  # noqa: S607
    capture_output=True,
    text=True,
    check=True,
)
version = output.stdout.strip()

subprocess.run(["git", "tag", version, "--force"], check=True)  # noqa: S603, S607
