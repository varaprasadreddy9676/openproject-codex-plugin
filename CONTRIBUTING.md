# Contributing

## Development

1. Copy `.mcp.json` and set your own `OPENPROJECT_BASE_URL`.
2. Create a token file, for example `~/.codex/secrets/openproject-api-token`.
3. Install dependencies:

```bash
python3 -m pip install -e .
```

3. Run the smoke test:

```bash
python3 ./scripts/smoke_test.py
```

## Guidelines

- Keep new tools small and composable.
- Prefer first-class wrappers for common workflows over forcing generic API calls.
- Do not commit secrets, real tokens, or organization-specific defaults.
- When adding write operations, include at least one read-only verification or mock-based test path.
