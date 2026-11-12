# Named project and demo publication

Date: 2026-10-20
Related plan: [Presentation materials](../plans/2026-10-18-presentation-materials.md)

## Changed

Published the current static presentation on the new Netlify account. The project page uses https://liutong-luban-project.netlify.app/ and the case replay page uses https://liutong-luban-demo.netlify.app/. Project/expert/reader replay routes redirect to the dedicated demo, whose return and architecture links target the project site. Both published sites are accessible without Netlify login. GitHub remains private for invited collaborators.

Updated the bilingual README, project-page and reader-console guides with these entry URLs and the publication date. Chinese display branding remains 六通智能体 and English branding remains Luban. Runtime code, experiment values and architecture artwork are unchanged.

## Files

README.md, README.zh-CN.md, docs/project-page.md, docs/project-page.zh-CN.md, docs/reader-console.md and docs/reader-console.zh-CN.md, plus the related implementation plan and task status.

## Verification

The existing website static check passed 13 HTML pages and 431 local links/assets/fragments, bilingual dictionaries and recorded-media contracts. Anonymous HTTP verification passed 49 resources across the two new sites, including eight replay recordings, replay JSON, scripts and both architecture SVGs. Four historical entry routes reached the demo root and a video byte-range request returned 206. Sources and media bytes were compared with the staged static files.

Local docs/checks use scripts/check_docs.py and git diff --check. The GitHub CPU verification workflow runs on the pushed revision. No new GPU experiment or local full runtime suite was needed for entry-link changes. Browser automation was unavailable during this deployment, so live browser playback was not rechecked in this pass.

## Remaining Work

No publication step remains. The private GitHub repository requires an invited account, and the user confirmed the advisor already has access.
