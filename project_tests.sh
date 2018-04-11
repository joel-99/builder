#!/bin/bash
set -e

. .activate-venv.sh
. .lint.sh
echo "Failing if lint produces local modifications"
git diff --exit-code
. .shell-lint.sh
deactivate

echo "Test with multiple Python versions and their virtualenvs"
tox
