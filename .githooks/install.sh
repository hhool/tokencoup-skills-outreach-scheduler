#!/bin/sh
# Enable the repo's git hooks (currently: the private-information gate).
#
#     sh .githooks/install.sh
#
# Git never auto-trusts hooks from a clone, so this has to be run once per
# machine. `core.hooksPath` is repo-local, so nothing outside this repo's
# .git/config is touched.
set -eu

REPO_DIR=$(git rev-parse --show-toplevel)
cd "$REPO_DIR"

chmod +x .githooks/pre-commit

git config core.hooksPath .githooks

echo "enabled: core.hooksPath=.githooks"
echo "verify:  git config core.hooksPath   # -> .githooks"
echo "test:    touch /tmp/x && git add /tmp/x   # should fail the scan"