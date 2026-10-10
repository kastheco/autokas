# test setup

before the first check in this checkout, run `bash scripts/setup-test-env.sh` once. it creates `.venv`, installs test dependencies and copies the example config only if `config.json` is absent. it needs Python 3 with venv support, not `uv` or credentials.

run scoped tests with `.venv/bin/python -m unittest <test_module> -v`. use `.venv/bin/python -m unittest discover -v` when full discovery is needed.
