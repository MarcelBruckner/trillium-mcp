# Contributing

- [Local development environment](#local-development-environment)
  - [Seeded instance credentials](#seeded-instance-credentials)
  - [Talking to the MCP server](#talking-to-the-mcp-server)
  - [Resetting the fixture](#resetting-the-fixture)
- [Running the tests](#running-the-tests)
  - [The easy way: run-tests.sh](#the-easy-way-run-testssh)
  - [Manually](#manually)
- [Tools generated from the ETAPI spec](#tools-generated-from-the-etapi-spec)

## Local development environment

The repo ships a ready-to-run dev stack: a throwaway Trilium instance seeded with
Trilium's **default demo notes**, plus the MCP server built from local source.

```bash
docker compose up -d --build
```

This starts two services (see [`docker-compose.yaml`](docker-compose.yaml)):

| Service        | URL                          | Notes                                   |
| -------------- | ---------------------------- | --------------------------------------- |
| `trilium`      | http://localhost:8080        | Web UI + ETAPI (`/etapi`)               |
| `trilium-mcp`  | http://localhost:8081/mcp    | MCP endpoint (built from local `Dockerfile`) |

The `trilium-mcp` service uses `build: .`, so it always runs your local code.
After changing anything under `app/`, rebuild with `docker compose up -d --build`.

Check the MCP server is up:

```bash
curl http://localhost:8081/health   # -> ok
```

### Seeded instance credentials

Both are committed as a **development fixture** and are scoped to this disposable
instance only — do not reuse them anywhere real.

- **Trilium web UI:** to log in to the seed instance at http://localhost:8080,
  use the password `trilium-mcp`.
- **ETAPI token:** in [`etapi.token`](etapi.token) — the credential the MCP client
  passes in the `Authorization` header.

The seeded database lives in `trilium-data/` (`config.ini` + `document.db`). Only
Trilium's default demo notes are present. Volatile per-run files (logs, tmp,
session secret, SQLite WAL/SHM) are gitignored so each checkout starts clean.

### Talking to the MCP server

Point any MCP client at the endpoint with the dev token:

```bash
claude mcp add trilium-dev --transport http \
  http://localhost:8081/mcp \
  --header "Authorization: $(cat etapi.token)"
```

### Resetting the fixture

To discard local changes to the seeded database and return to the committed state:

```bash
docker compose down
git checkout -- trilium-data/
docker compose up -d
```

## Running the tests

Tests live in `app/tests/` and come in two kinds:

- **Unit tests** (`app/tests/test_*.py`) run against a mock ETAPI transport — no
  running Trilium required.
- **Live integration tests** (`app/tests/live/`) drive the real MCP server over
  HTTP against the running stack, covering every MCP tool. They **auto-skip** when
  the MCP `/health` endpoint is unreachable.

### The easy way: `run-tests.sh`

[`run-tests.sh`](run-tests.sh) runs the whole suite against a **clean, disposable
fixture**. It stops the containers, resets the seeded DB (with the containers
down so SQLite isn't corrupted), rebuilds and starts them, waits for MCP health,
runs `pytest`, then stops the containers and resets the DB again — even if a test
fails. Extra arguments pass through to `pytest`:

```bash
./run-tests.sh                  # full suite against a fresh fixture
./run-tests.sh tests/live -q    # just the live tests, quietly
./run-tests.sh -k export        # a single test by keyword
```

It leaves the stack **stopped** at the end; run `docker compose up -d` to bring
it back for interactive work.

### Manually

```bash
cd app
uv run pytest        # or: .venv/bin/python -m pytest
```

Without the stack up, the live tests skip and the unit tests still run. With the
stack up (`docker compose up -d`), the full suite runs — but note the live tests
mutate the fixture, so `git checkout -- trilium-data/` afterwards (or just use
`run-tests.sh`, which handles the reset for you).

## Tools generated from the ETAPI spec

Most MCP tools are generated automatically from
[`app/trilium-etapi.openapi`](app/trilium-etapi.openapi) via
`FastMCP.from_openapi`. Endpoints that don't fit the JSON-in/JSON-out mold are
handled specially in [`app/server.py`](app/server.py) — for example
`exportNoteSubtree` returns a binary ZIP, so the generated tool is excluded and
replaced with one that unpacks the archive into readable text. If you add
similar special handling, cover it with a test in `app/tests/`.
