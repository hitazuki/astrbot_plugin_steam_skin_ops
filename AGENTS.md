# Repository instructions

## Project scope

This repository is the standalone AstrBot plugin extracted from the former
`buff2steam` / `steam-skin-ops` project. Keep it a pure in-process AstrBot
plugin.

- Do not reintroduce the former FastAPI, Uvicorn, Docker Compose, callback API,
  or standalone service deployment.
- Keep AstrBot integration in `main.py` and reusable market, forecast, storage,
  and rule logic under `core/`.
- Support Python `>=3.10` and AstrBot `>=4.16,<5` unless a deliberate release
  changes both the implementation and declared compatibility.
- Keep runtime dependencies synchronized between `requirements.txt` and
  `pyproject.toml`.

## Persistent data and compatibility

- Store runtime state only below
  `data/plugin_data/astrbot_plugin_steam_skin_ops/` through AstrBot's plugin
  data-path API. Never store mutable state in the installed plugin directory.
- Preserve existing rules, alert history, and SQLite data during normal plugin
  updates.
- Make schema changes forward-migrating and covered by tests. Reject databases
  created by a newer unsupported schema instead of modifying them.
- Do not silently import or modify the former standalone service's `monitor.db`.
- Preserve `unified_msg_origin` / recipient isolation: one conversation must
  not read, modify, or receive another conversation's rules or alerts.

## Alert and forecast behavior

- The supported rule identifiers are `ratio`, `t7`, `platform`, and `steam`.
  Treat them as persisted public API values.
- `t7` must use the exact dynamic seven-day forecast result displayed by
  `/skin quote`; do not maintain a separate prediction or rounding path for
  alert evaluation.
- Risk assessment is explanatory unless a deliberate, documented rule change
  makes a risk value part of a trigger.
- Changes to thresholds, hysteresis, breakthrough levels, health alerts, daily
  summaries, forecast selection, or price inputs require focused regression
  tests and matching user-facing documentation.
- Missing, stale, or insufficient data must not create a false trading alert.

## Validation

Install development dependencies and run all checks before committing:

```text
python -m pip install -e ".[dev]"
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q main.py core
python scripts/validate_market_package.py
```

Do not weaken or skip an existing check to make a change pass. The generated
market archive must remain below 16 MiB and contain files at the archive root,
not inside an extra parent directory.

## Release versioning

For a release, keep the version synchronized in:

- `metadata.yaml`
- `pyproject.toml`
- `scripts/validate_market_package.py`

Move relevant entries from `CHANGELOG.md`'s `Unreleased` section into a dated
release section. The plugin-market record, when used, must carry the same
version as `metadata.yaml`. Regenerate and validate
`dist/astrbot_plugin_steam_skin_ops.zip` before publishing a tag.

## Commit conventions

All commits must use Conventional Commits with a required scope:

```text
<type>(<scope>): <description>
```

Allowed types are `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, and `revert`.

- Use a lowercase kebab-case scope, such as `rules`, `forecast`, `quote`,
  `smis-client`, `storage`, `astrbot-plugin`, or `release`.
- Write a concise description after the colon and space.
- Add `!` before the colon for a breaking change, for example
  `feat(rules)!: redefine the t7 threshold basis`.
- Apply the same format to pull request titles because squash merges use the
  pull request title as the resulting commit subject.
- Preserve unrelated working-tree changes and stage only files intended for
  the commit.

Examples:

- `feat(rules): use dynamic forecast for t7 alerts`
- `fix(quote): mark every equal lowest platform price`
- `docs(deploy): clarify WebUI test updates`
- `test(storage): cover t7 state migration`

Before creating a commit, validate its subject with:

```text
python scripts/check_commit_message.py --message "feat(scope): description"
```

Enable the repository hook in a new clone with:

```text
git config core.hooksPath .githooks
```
