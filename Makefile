PYTHON ?= python3

.PHONY: test demo check schemas
test:
	$(PYTHON) -m unittest discover -s tests -t . -v

demo:
	$(PYTHON) -m examples.demo

check: test
	$(PYTHON) scripts/check_schemas.py
	$(PYTHON) -m examples.demo

schemas:
	$(PYTHON) scripts/check_schemas.py --write
