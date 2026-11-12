# Private GitHub preparation

Date: 2026-10-07
Status: DONE

## Goal

Prepare the current Final agent working tree as an independently installable and reviewable repository. The GitHub repository must remain Private. Public release is not authorized.

## Scope

Preserve core implementation, tests, scripts, configuration and small example resources. Exclude third-party source trees, model weights, generated output, credentials and internal operational records from tracked distribution. Preserve source provenance and Apache-2.0 attribution. Do not modify original source, shared environments or existing processes.

## Tasks

- [x] Audit actual interfaces, dependencies and runtime resources.
- [x] Repair independent installation, configuration and package resources.
- [x] Validate offline example, fast checks, wheel/sdist and one bounded real episode.
- [x] Rewrite usage docs, audit licenses and tracked content.
- [x] Create fresh Git history, verify Private before and after push.

## Acceptance Criteria

A new Python 3.11 environment installs the project; offline example runs without services; a bounded real simulation episode has an honest result and inspectable artifacts or a concrete blocker. All fast checks and distribution checks have current evidence. No secret, weight or copied unlicensed source is tracked. GitHub visibility is Private at both push gates.

## Results

Completed private preparation and initial push on 2026-10-08. GitHub API verified private=true before and after push; fresh clone installation, offline CLI and 956 passed / 14 skipped confirmed the actual pushed tree. Full scope and remaining public-release decisions are recorded in the change record and docs/validation.md.
