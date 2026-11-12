# Tasks

- [ ] `IN_PROGRESS` 固定表9-13送测发布：统一复测96/96成功，30项新组合30/30，末尾四类M12物理扰动均在10秒内恢复。RoboCasa100回合重跑完成，基座43/100、Luban55/100，无运行异常，相对增益27.91%。VLABench固定50回合记录为50/50与0/50，官方参考OpenVLA权重低成功率问题已查明。两套独立核心环境均5/5部署通过，真实HTTP调用100/100，接入额外耗时P95为0.506秒，3721秒稳定性测试153/153，17项异常测试通过。独立公开仓库yuliu-ai/Luban-Milestone-2026-12已发布，服务器匿名下载及原生安装成功，公开源码96次原子技能调用、30项组合和四类恢复均通过。第一套六环境安装完成且均不共享系统site-packages，第二套六环境安装中，RoboCasa100与后续VLABench50由服务器独立队列执行，两份8页测评Word已生成并完成逐页排版检查（[计划](plans/2026-11-12-luban-milestone-release.md)）。

- [ ] `IN_PROGRESS` Configurable one- and two-branch execution and a simulator-informed single-agent evaluation ([plan](plans/2026-11-10-single-branch-simulator-info.md)).

- [ ] `IN_PROGRESS` XR-1 dual-branch Composite-Unseen evaluation ([plan](plans/2026-11-10-xr1-dual-unseen.md)).

- [x] `DONE` Luban package identity and bilingual user guides ([plan](plans/2026-10-08-luban-package-bilingual.md)).

- [x] `DONE` Standardize LIBERO report labels and metric definitions ([plan](plans/2026-10-08-report-labels.md)).

- [x] `DONE` README research layout, centered branding and reader-console entry ([plan](plans/2026-10-08-readme-research-layout.md)).

- [ ] `PLANNED` Next stage: asynchronous execution; current version remains unpublished ([plan](plans/2026-10-08-next-stage-async.md)).

- [x] `DONE` Luban presentation and GPU onboarding ([plan](plans/2026-10-08-release-presentation.md)). The 64-GIF gallery and report-results expansion is complete ([record](changes/2026-10-28-atomic-gallery-and-results.md)). Compute-resource profiles were expanded on 2026-10-28 ([record](changes/2026-10-28-compute-requirements.md)).

- [x] `DONE` New-machine readiness and private project page — Luban bilingual presentation, full CPU/cloud verification, new Private organization repository and active Read access completed ([plan](plans/2026-10-08-new-machine-readiness.md)).

- [x] `DONE` Private GitHub preparation ([plan](plans/2026-10-07-private-github-preparation.md)).

The organization repository contains only reviewed independent history. The owner
selected a new Private repository and preservation of the personal Private
repository as a backup; legacy main history was excluded, without force push.
Private preparation has no remaining history decision. Future public release still
requires the owner's separate authorization and fixture/attribution review in
THIRD_PARTY_NOTICES.md. Historical evaluations are not acceptance evidence here.

- [x] `DONE` Presentation-material update and named project/replay publication, with bilingual GitHub entry synchronization ([plan](plans/2026-10-18-presentation-materials.md)).

- [x] `DONE` 本地GitHub首页补充演示与汇报结果，未推送或部署（[记录](changes/2026-10-21-github-showcase-local.md)）。

- [x] DONE 2026-10-23: Bilingual presentation and RoboSyn adapter passed Tailscale server checks, including 1,012 tests and independent distribution validation ([plan](plans/2026-10-23-github-framework-refresh.md)).
