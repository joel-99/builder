#!/bin/bash

set -e # everything must pass

envname="${1}"

if [ -z "$envname" ]; then
    echo "Must pass a label for test artifact file e.g. py27"
    exit 1
fi

echo "Running tests"
export PYTHONPATH="src"
pytest \
    -n 4 \
    -s \
    --junitxml=build/pytest-$envname.xml \
    src/tests src/integration_tests

echo "Checking coverage report"
coverage report --fail-under=50
