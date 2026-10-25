SHELL := /bin/bash
PY ?= python3
BASH_SCRIPTS := proxmox/pve-snapshot-rotate.sh kvm/cloud-init-vm.sh
PY_SCRIPTS := bgp/anycast-healthcheck.py vmware/vcenter-snapshot-report.py

.PHONY: all lint shellcheck bashn ruff compile test help-check clean

all: lint test

lint: shellcheck bashn ruff compile

shellcheck:
	shellcheck -S style $(BASH_SCRIPTS)

bashn:
	@for f in $(BASH_SCRIPTS); do bash -n $$f && echo "bash -n ok: $$f"; done

ruff:
	@command -v ruff >/dev/null && ruff check $(PY_SCRIPTS) tests || echo "ruff not installed, skipping (pip install ruff)"

compile:
	$(PY) -m py_compile $(PY_SCRIPTS) tests/*.py

test:
	$(PY) -m unittest discover -s tests -v

help-check:
	@for f in $(BASH_SCRIPTS) $(PY_SCRIPTS); do ./$$f --help >/dev/null && echo "--help ok: $$f"; done

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
