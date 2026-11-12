# Luban package identity and bilingual user guides

Date: 2026-10-08
Related plan: [package and bilingual guides](../plans/2026-10-08-luban-package-bilingual.md)

## Changed

Renamed the installable distribution, Python namespace, CLI and owned environment
prefixes to luban. Updated imports, lazy exports, dynamic config class paths,
launchers, provenance metadata, tests, lockfile, resources and CI commands.
Display branding is Luban; mandatory upstream attribution and historical source
records retain their real identities. Independent atomic-skill/Chemistry packages
retain their existing boundaries. No compatibility alias is advertised.

Removed the five specified roadmap bullets in both README languages. Added 19
complete English/Chinese detailed-guide pairs, plus paired configuration index,
contribution, security, reproduction, citation and third-party notices. Added
paired MkDocs/sidebar/README navigation and checks for counterpart existence,
correct language switching, substantive Chinese content and identical code blocks.

## Files

- src/luban replaces the previous primary runtime package directory.
- pyproject.toml, uv.lock, configs, owned scripts/tests, CI and packaging metadata.
- README pair, docs EN/ZH pairs, root supporting guide pairs, configs/README pair.
- scripts/check_docs.py, scripts/check_distribution.py, MANIFEST.in and architecture.
- Owned catalog display metadata and builder output naming; original source input retained.

## Verification

- Frozen all-extras/dev/docs/full-test install in a new isolated environment passed.
- Full migrated CPU suite: 973 passed, zero skipped; Ruff/format/strict mypy passed.
- 35 configuration constructors and both catalog/bundle validation sets passed.
- `luban --help`, `python -m luban`, health/run and offline module passed.
- Wheel independently installed outside checkout, with distribution-name, typed
  resource, Chemistry protobuf/YAML and executable CLI checks passed.
- Independent source-archive install and complete 973-test suite passed, zero skips.
- EN/ZH code-block, guide presence, language-target, local-link and strict MkDocs checks passed.
- Real GPU migrated entry: CloseBlenderLid, target/index 0/seed 7, budgets 600 steps,
  38 chunks/600 seconds. Native task_success at 352 steps/22 chunks, zero exceptions.
  Session 18c04c6f-a2f8-41ae-9bfa-3bef5ff93773 has results, traces, manifest and video.
- Only the owned temporary GPU service was stopped; original source/shared environments unchanged.
- Five roadmap removals checked; credential exact-match/large-file scan passed.
- GitHub workflow and final exact-commit CI are verified during Private push.

## Remaining Work

Asynchronous execution remains planned for a later stage. No public release,
website deployment or repository-visibility change is part of this migration.
