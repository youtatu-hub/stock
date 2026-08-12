# Repository Guidelines

## Project Structure & Module Organization

This is a Windows-oriented Python stock-analysis service. `app.py` hosts the HTTP API and dashboard on `127.0.0.1:8795`; `launcher.py` handles licensing and startup. Keep market-data access and models in `data/`, and algorithms in `analysis/` (trend, patterns, volume/price, CAN SLIM, breakout, and Chanlun). Static UI files live in `dashboard/`, notes in `docs/`, and vendored packages in `libs/`. Treat `data/license.dat`, logs, and `__pycache__/` as runtime artifacts.

## Build, Test, and Development Commands

- `py -3 app.py` starts the API and dashboard directly for development, bypassing the licensed launcher.
- `调试启动.bat` provides the same debug startup from Windows Explorer.
- `启动.bat` runs the normal licensed flow through `launcher.py`.
- `py -3 -m compileall app.py launcher.py license_core.py gen_license.py analysis data` performs a fast syntax/import-bytecode check.
- Open `http://127.0.0.1:8795/api/health` for a minimal service smoke test.

There is no build system or dependency manifest. Avoid modifying `libs/`; it is the portable dependency bundle.

## Coding Style & Naming Conventions

Use four-space indentation and PEP 8. Name functions, modules, and variables with `snake_case`; classes with `PascalCase`; constants with `UPPER_SNAKE_CASE`. Preserve type hints and `from __future__ import annotations` where present. Keep API handlers thin and reusable logic in its owning package. Files are UTF-8; limit comments to non-obvious decisions.

## Testing Guidelines

No automated test suite is checked in. Add tests under `tests/`, named `test_<module>.py`, using deterministic fixtures instead of live market requests. Cover indicator calculations, thresholds, serialization, and invalid query parameters. Run them with `py -3 -m unittest discover -s tests -p "test_*.py"`, then smoke-test `/api/health` and affected endpoints.

## Commit & Pull Request Guidelines

Git history is unavailable in this copy, so use concise imperative commits such as `fix: clamp invalid kline counts` or `feat: add weekly signal summary`. Keep each commit scoped to one behavior. Pull requests should describe the change, affected modules, verification, and data or license implications. Include screenshots for UI changes and sample payloads for API changes.

## Security & Configuration

Never commit signing keys, customer licenses, credentials, or captured market data. Keep the service bound to localhost unless authentication is added. Validate inputs at API boundaries and bound network requests.
