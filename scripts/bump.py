# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "commitizen",
#     "git-changelog",
# ]
# ///
import subprocess

subprocess.run(["cz", "bump"], check=True)
subprocess.run(["git-changelog"], check=True)
subprocess.run(["uv", "lock"], check=True)
subprocess.run(["git", "add", "CHANGELOG.md", "uv.lock"], check=True)
subprocess.run(["git", "commit", "--amend", "--no-edit"], check=True)

output = subprocess.run(
    ["cz", "version", "--project"],
    capture_output=True,
    text=True,
    check=True,
)
version = output.stdout.strip()

subprocess.run(["git", "tag", version, "--force"], check=True)
