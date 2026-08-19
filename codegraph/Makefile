# codegraph - deterministic multi-root code knowledge graph for coding agents
#
# Quick start:
#   make install           create the venv and install the graphify backend
#   make init ROOT=../src  create a workspace indexing that directory
#   make build             build the graph
#   make install-claude    wire the MCP server + both hooks into Claude Code
#
# The backend (tree-sitter, grammars) lives in a local venv. codegraph itself,
# the MCP server and both hooks are pure standard library and run on any
# Python 3.10+ - which is why they keep working if the venv is missing.

SHELL      := /bin/sh
.SHELLFLAGS := -eu -c
.DEFAULT_GOAL := help

# --- paths ------------------------------------------------------------------
MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
HERE          := $(patsubst %/,%,$(dir $(MAKEFILE_PATH)))
VENV          := $(HERE)/.venv
VENV_PY       := $(VENV)/bin/python
VENV_PIP      := $(VENV)/bin/pip
CG            := $(HERE)/scripts/codegraph
SRC           := $(HERE)/src

# System Python runs codegraph itself; override if 'python3' is not 3.10+.
PYTHON  ?= python3
# Backend interpreter: the venv if it exists, else whatever the environment
# already points at. Snapshotted with := because the recipes re-export it under
# the same name, and a recursive default would reference itself.
BACKEND_PYTHON_ENV := $(CODEGRAPH_BACKEND_PYTHON)
BACKEND_PYTHON ?= $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(BACKEND_PYTHON_ENV))

# Workspace to operate on. Defaults to the repository containing this plugin.
WORKSPACE ?= $(abspath $(HERE)/..)
ROOT      ?=
NAME      ?=
PLATFORM  ?= claude
SCOPE     ?= project

RUN = CODEGRAPH_BACKEND_PYTHON="$(BACKEND_PYTHON)" PYTHONPATH="$(SRC)" $(PYTHON) -m codegraph --workspace "$(WORKSPACE)"

.PHONY: help install install-graphify install-crg install-all venv \
        init add-root build rebuild incremental status doctor context serve \
        install-claude install-cursor install-codex install-vscode install-gemini \
        apm-pack test demo clean distclean uninstall print-config

# --- help -------------------------------------------------------------------
help:
	@printf '\033[1mcodegraph\033[0m - deterministic code knowledge graph for coding agents\n\n'
	@printf '\033[1msetup\033[0m\n'
	@printf '  make install            venv + graphify backend (default, no API key needed)\n'
	@printf '  make install-crg        also install the code-review-graph backend\n'
	@printf '  make install-all        install both backends\n'
	@printf '  make doctor             check interpreters, backends and workspace health\n\n'
	@printf '\033[1mgraph\033[0m\n'
	@printf '  make init ROOT=<path>   create a workspace indexing <path>\n'
	@printf '  make add-root ROOT=<p>  index another codebase (linked by synthetic edges)\n'
	@printf '  make build              full deterministic rebuild\n'
	@printf '  make incremental        re-parse only changed files\n'
	@printf '  make status             what is indexed, and how stale\n'
	@printf '  make context Q="..."    show what the prompt hook would inject\n\n'
	@printf '\033[1massistants\033[0m\n'
	@printf '  make install-claude     MCP server + both hooks + CLAUDE.md section\n'
	@printf '  make install-cursor     .cursor/mcp.json + rule\n'
	@printf '  make install-codex      .mcp.json + AGENTS.md section\n'
	@printf '  make install-vscode     .vscode/mcp.json + copilot-instructions\n'
	@printf '  make apm-pack           emit the APM package manifest\n\n'
	@printf '\033[1mdev\033[0m\n'
	@printf '  make test               run the test suite\n'
	@printf '  make demo               end-to-end walkthrough on the bundled example\n'
	@printf '  make serve              run the MCP server on stdio (for debugging)\n'
	@printf '  make clean              remove generated graph data\n\n'
	@printf 'variables: WORKSPACE=%s\n' '$(WORKSPACE)'
	@printf '           PYTHON=%s  BACKEND_PYTHON=%s\n' '$(PYTHON)' '$(BACKEND_PYTHON)'

print-config:
	@printf 'HERE=%s\nWORKSPACE=%s\nPYTHON=%s\nBACKEND_PYTHON=%s\nVENV=%s\n' \
		'$(HERE)' '$(WORKSPACE)' '$(PYTHON)' '$(BACKEND_PYTHON)' '$(VENV)'

# --- install ----------------------------------------------------------------
venv: $(VENV_PY)

$(VENV_PY):
	@printf '\033[1m==>\033[0m creating venv at %s\n' '$(VENV)'
	@$(PYTHON) -m venv "$(VENV)"
	@"$(VENV_PIP)" install --quiet --upgrade pip setuptools wheel
	@printf '    %s\n' "$$("$(VENV_PY)" --version)"

## Default backend. --code-only AST extraction needs no API key and makes no
## network calls at build time.
install-graphify: venv
	@printf '\033[1m==>\033[0m installing graphify (Apache-2.0, see THIRD_PARTY_NOTICES.md)\n'
	@"$(VENV_PIP)" install --quiet --upgrade graphifyy
	@"$(VENV_PY)" -c "import graphify; print('    graphify ready')"

install-crg: venv
	@printf '\033[1m==>\033[0m installing code-review-graph (MIT, see THIRD_PARTY_NOTICES.md)\n'
	@"$(VENV_PIP)" install --quiet --upgrade code-review-graph
	@"$(VENV_PY)" -c "import code_review_graph; print('    code-review-graph ready')"

install: install-graphify
	@printf '\n\033[1mnext:\033[0m make init ROOT=<path-to-your-code>\n'

install-all: install-graphify install-crg
	@printf '\n\033[1mnext:\033[0m make init ROOT=<path-to-your-code>\n'

# --- graph ------------------------------------------------------------------
init:
	@test -n "$(ROOT)" || { printf 'usage: make init ROOT=<path> [NAME=<name>]\n'; exit 2; }
	@$(RUN) init "$(ROOT)" $(if $(NAME),--name "$(NAME)",)
	@$(MAKE) --no-print-directory build

add-root:
	@test -n "$(ROOT)" || { printf 'usage: make add-root ROOT=<path> [NAME=<name>]\n'; exit 2; }
	@$(RUN) add-root "$(ROOT)" $(if $(NAME),--name "$(NAME)",)
	@$(MAKE) --no-print-directory build

build:
	@$(RUN) build

rebuild: build

incremental:
	@$(RUN) build --incremental

status:
	@$(RUN) status

doctor:
	@$(RUN) doctor --backend-python "$(BACKEND_PYTHON)"

context:
	@test -n "$(Q)" || { printf 'usage: make context Q="how does X work?"\n'; exit 2; }
	@$(RUN) context --prompt "$(Q)"

serve:
	@$(RUN) serve

# --- assistant integration --------------------------------------------------
install-claude:
	@$(RUN) install --platform claude --scope "$(SCOPE)"

install-cursor:
	@$(RUN) install --platform cursor --scope "$(SCOPE)"

install-codex:
	@$(RUN) install --platform codex --scope "$(SCOPE)"

install-vscode:
	@$(RUN) install --platform vscode --scope "$(SCOPE)"

install-gemini:
	@$(RUN) install --platform gemini --scope "$(SCOPE)"

apm-pack:
	@printf '\033[1m==>\033[0m APM package manifest: %s\n' '$(HERE)/apm/apm.yml'
	@cat "$(HERE)/apm/apm.yml"
	@printf '\ninstall with:  apm install\n'

# --- dev --------------------------------------------------------------------
test:
	@PYTHONPATH="$(SRC)" $(PYTHON) -m pytest "$(HERE)/tests" -q

demo:
	@PYTHONPATH="$(SRC)" CODEGRAPH_BACKEND_PYTHON="$(BACKEND_PYTHON)" \
		$(PYTHON) "$(HERE)/scripts/demo.py"

clean:
	@rm -rf "$(WORKSPACE)/.codegraph/graph.json" "$(WORKSPACE)/.codegraph/index.json" \
		"$(WORKSPACE)/.codegraph/state.json" "$(WORKSPACE)/.codegraph/roots" \
		"$(WORKSPACE)/.codegraph/rebuild.lock" "$(WORKSPACE)/.codegraph/pending.json"
	@find "$(HERE)" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@printf 'removed generated graph data (config.json and annotations.jsonl kept)\n'

distclean: clean
	@rm -rf "$(VENV)"
	@printf 'removed the backend venv\n'

uninstall:
	@printf 'to remove codegraph from an assistant, delete its block:\n'
	@printf '  .mcp.json                -> "codegraph" under mcpServers\n'
	@printf '  .claude/settings.json    -> the codegraph hooks entries\n'
	@printf '  CLAUDE.md / AGENTS.md    -> the <!-- codegraph:begin --> block\n'
	@printf 'then: make distclean\n'
