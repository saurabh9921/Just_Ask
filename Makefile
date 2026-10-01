# ERP Insight AI - thin wrapper over scripts/dev.py (works without make, too:
#   python scripts/dev.py <target>)

PY ?= python
DEV := $(PY) scripts/dev.py

.PHONY: help bootstrap install format lint typecheck test test-security \
        test-performance test-integration eval audit check up up-data down logs \
        migrate migrate-sql \
        ext-build api

help:             ; @$(DEV) help
bootstrap:        ; @$(DEV) bootstrap
install:          ; @$(DEV) install
format:           ; @$(DEV) format
lint:             ; @$(DEV) lint
typecheck:        ; @$(DEV) typecheck
test:             ; @$(DEV) test
test-security:    ; @$(DEV) test-security
test-performance: ; @$(DEV) test-performance
eval:             ; @$(DEV) eval
audit:            ; @$(DEV) audit
test-integration: ; @$(DEV) test-integration
check:            ; @$(DEV) check
up:               ; @$(DEV) up
up-data:          ; @$(DEV) up-data
down:             ; @$(DEV) down
logs:             ; @$(DEV) logs
migrate:          ; @$(DEV) migrate
migrate-sql:      ; @$(DEV) migrate-sql
ext-build:        ; @$(DEV) ext-build
api:              ; @$(DEV) api
