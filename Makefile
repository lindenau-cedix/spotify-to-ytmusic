PY ?= python3
PIP ?= $(PY) -m pip
SRC := src

.PHONY: install install-dev test run serve auth-status lint fmt clean help

help:
	@echo "spotify-to-ytmusic"
	@echo "  make install      Install runtime deps"
	@echo "  make install-dev  Install runtime + dev deps"
	@echo "  make test         Run pytest"
	@echo "  make run          End-to-end CLI (use with PLAYLIST=<id>)"
	@echo "  make serve        Start local web UI on :8000"
	@echo "  make run-auth     Walk through both auth flows"
	@echo "  make auth-status  Show whether Spotify/YTM are authenticated"
	@echo "  make clean        Remove caches and logs"

install:
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

install-dev:
	$(PIP) install -r requirements.txt
	$(PIP) install -e .[dev]

test:
	$(PY) -m pytest -q

run:
	$(PY) -m migrator run $(PLAYLIST)

serve:
	$(PY) -m migrator serve

run-auth:
	$(PY) -m migrator auth spotify
	$(PY) -m migrator auth ytm

auth-status:
	$(PY) -m migrator auth status

lint:
	$(PY) -m compileall -q $(SRC)

clean:
	rm -rf logs/*.log .pytest_cache src/*.egg-info build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +