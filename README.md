# PromptGate

PromptGate is a CAB432 Assignment 3 prompt regression testing service.
Task 1 provides a health-only API and pure key and access helpers.
These components run locally without AWS credentials or settings.

## Requirements

- Python 3.14; the local virtual environment uses Python 3.14.6.
- Dependencies pinned in `requirements.txt`.

## Run locally (PowerShell)

From the `promptgate` directory:

```powershell
.\.venv\Scripts\python -m api.main
```

The server listens on port 3000. Open `http://127.0.0.1:3000/health`
for `{"status":"ok"}`. Local only, no AWS.

## Tests (PowerShell)

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

The health test clears environment settings and blocks boto3 client and
resource factories. The key and access tests use pure functions.

## Health image (local only, no AWS)

Start Docker Desktop with the Linux engine running. From `promptgate`:

```powershell
docker build -t n10910433-a3-promptgate:health .
docker run --rm -p 3000:3000 n10910433-a3-promptgate:health
```

Keep that terminal open. In a second PowerShell window:

```powershell
$task1Health = Invoke-WebRequest -Uri 'http://127.0.0.1:3000/health'
$task1Health.StatusCode
$task1Health.Content
```

Expected: HTTP `200` and `{"status":"ok"}`. Use Ctrl+C in the container
terminal when finished; `--rm` removes that container after it exits.

The image runs as `10001:10001`, copies only `core`, `api` and `worker`,
and starts the API with an exec-form command so signals reach Python.
The build context excludes Git metadata, the virtual environment,
tests, development folders, environment files and CSVs outside `samples`.

## Design

- `core/keys.py` centralises key formatting. Every partition key includes
  its owner, and versions use at least four digits of zero padding.
- The `FRESH#` pointer is the implementation plan's addition for later
  `auto` mode. It includes both configuration and suite versions.
- `core/access.py` accepts a verified caller's `sub`, validates the whole
  resource ID, and resolves private lookups to that caller. `pub_` IDs
  resolve to owner `public` for reads; writes are denied. This helper does
  not verify tokens or fetch resources.
- `api/main.py` defines only `/health`. Server startup stays inside
  `main()`, and this module creates no AWS clients.
- `worker/__init__.py` is an empty package marker for the shared image.

Container work follows
[6.31 Practical: Getting Started with Docker](https://canvas.qut.edu.au/courses/23778/pages/6-dot-31-practical-getting-started-with-docker?module_item_id=2372143)
and the existing Assignment 2 Python image pattern.
