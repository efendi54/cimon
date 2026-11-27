# Contributing

## Development tools

- [Commitizen](https://commitizen-tools.github.io/commitizen/)
- [Ruff](https://docs.astral.sh/ruff/)
- [pre-commit](https://pre-commit.com/)
- [actionlint](https://github.com/rhysd/actionlint): optional and separately
  installed for checking GitHub Actions workflows locally; it is not a Python dependency.

Commitizen, Ruff and pre-commit are included in the uv `dev` group.

## Pull request tests

The [PR workflow](.github/workflows/pr.yml) runs the full test suite when a
pull request is opened, reopened, or updated with commits. It tests GitHub's
PR merge commit with Python 3.12, including the `viz` extra. To require the
tests before merging, configure `cimon full test suite` as a required status
check in the repository's ruleset or branch protection rules.
The `cimon workflow lint` check runs actionlint on all workflow files and can
also be configured as a required status check.

## Commit convention

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
messages, for example `feat: add metrics export` or `fix: handle missing jobs`.
Mark breaking changes with `!`, for example `feat!: change output format`, or
with a `BREAKING CHANGE:` footer. Commitizen uses these messages to determine
version changes and generate changelog entries; changes without a recognized
conventional message may not be reflected in the version or changelog.

The automatic workflow starts a bump only when it finds a `feat`, `fix`, or
breaking change since the current version tag. Other types such as `docs` or
`refactor` alone do not trigger it. For squash merges, ensure the final squash
commit message follows the convention.

The PR workflow checks every commit reachable from the PR head but not the
base using Commitizen. Configure `cimon conventional commits` as a required
status check in the repository's ruleset to enforce this before merging.

To check staged Python files with Ruff and commit messages with Commitizen
before Git creates a commit, install the hooks from the checkout root:

```bash
uv run --locked pre-commit install
```

Ruff checks staged Python files using `ruff.toml`; it does not automatically
change them. Commitizen uses Git's `commit-msg` stage, when the message is
available. Run `uv run --locked pre-commit install` again in an existing
checkout to install both stages. If this checkout previously used the old
`.githooks` setup, remove that specific
configuration first with `git config --local --unset core.hooksPath`; review
any other custom hooks before changing their configuration. The hooks run
outside Bazel because they are invoked by Git and use the dev dependencies
resolved by uv from the configured indexes. They can be bypassed with
`git commit --no-verify`. To check existing commits locally, fetch the base
branch and run:

```bash
uv run --locked cz check --rev-range origin/main..HEAD
```

To lint workflow files locally after installing actionlint:

```bash
actionlint
```

## Automatic version bumps

The [version-bump workflow](.github/workflows/version-bump.yml) runs after a
push to `main`. It first checks every commit since the current version tag
with Commitizen, including squash commits. A validation failure stops the
workflow without creating a tag. If there is a `feat`, `fix`, or breaking
change, it then runs the test suite on the checked-out `main` commit, including
the `viz` extra. Only if the tests pass does Commitizen update the version,
lockfile and changelog and push the bump commit and `v<version>` tag together.
If there are no qualifying commits, no tag is created. Concurrent pushes can
be included in the same bump.

If a commit message is invalid, inspect the workflow log and coordinate the
correction with collaborators before rewriting shared `main` history. An
authorized user must correct the affected commits after the current version
tag and push the rewritten branch, using any necessary temporary ruleset
bypass. Do not rewrite or move the current version tag. Verify the corrected
range with `uv run --locked cz check --rev-range v<version>..HEAD` before
pushing; use `git push --force-with-lease` rather than an unconditional force
push, then restore protections and confirm the new workflow run succeeds.
History rewriting changes commit IDs and requires collaborators to synchronize
their branches. Check the failure output first: not every validation failure
is caused by an invalid message.

The workflow uses a GitHub App installation token to push the bump commit and
tag. To configure it:

1. Create a GitHub App with repository **Contents: read and write** permission,
   install it only on this repository, and generate a private key.
2. Add the App ID as the repository Actions variable `CIMON_BUMP_APP_ID` and
   the complete private key as the Actions secret `CIMON_BUMP_APP_PRIVATE_KEY`.
   Keep the private key out of the repository and chat.
3. Add the app to the bypass list of every applicable `main` ruleset with
   direct-push bypass permission. Also allow it through any applicable branch
   protection and tag rules. This grants the app the ability to bypass those
   protections, so limit access to its private key.

The workflow creates a repository-scoped token only after the tests pass.
A competing push can cause the atomic push to fail; a later workflow run
processes the updated `main`. The App push triggers another workflow run,
which sees the current commit is already tagged and does nothing. No GitHub
Release is created.

## Creating a pull request

Install the GitHub CLI and authenticate with `gh auth login`. From a cimon Git
checkout, commit your changes on a feature branch, then run:

```bash
gh pr create --template pull_request_template.md
```

The [PR template](.github/pull_request_template.md) must be present on the
repository's default branch. `gh` uses the current branch and guides you
through creating the pull request, offering to push the branch if needed.
Fill in the summary, changes, and validation commands and results.

For squash merges, use a Conventional Commit PR title such as
`feat: add metrics export` so the resulting commit can trigger the automatic
version bump when appropriate.
