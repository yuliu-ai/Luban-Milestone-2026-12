# Luban package identity and bilingual user guides

Status: DONE

## User scope

Remove the five specified roadmap items from both README languages. Migrate the
actual installable distribution, Python namespace and command to luban, including
configs, owned environment-variable prefixes, scripts, tests and package resources.
Do not leave documentation-only branding over an old runtime namespace.

Provide complete English/Chinese versions of every detailed guide linked from the
README, with paired navigation and synchronized executable examples. Preserve real
upstream identities and required copyright/source attribution; source records are
not renamed into false Luban authorship. No release or visibility change.

## Acceptance

- Existing tests, import boundaries, configuration loading, lint/types and both CLI
  entry paths work through luban; frozen dependency resolution remains reproducible.
- Independent wheel and source-archive installs include the renamed package/resources.
- CPU examples run from a clean environment and outside the checkout.
- All user guide pages have substantive EN/ZH counterparts and valid links.
- Five roadmap entries removed; old runtime/package branding absent from active usage.
- A bounded GPU episode checks the new entry using available existing resources.
- Private push and final repository CI are verified on the resulting commit.
