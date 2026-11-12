# XR-1 dual-branch Composite-Unseen evaluation

Status: IN_PROGRESS

Use the first 30 recorded XR-1 official episodes for each Composite-Unseen task
with RoboCasa 1.0.1 and pretrain scenes. Run two independent simulations per
episode with the same environment seed and distinct deterministic policy seeds.
An authoritative success terminates the other branch. Two completed failures give
one failed episode, while runtime errors remain separate.

Each branch uses 2.5 times the official task horizon. The planner and verifier
use GPT-6.1 Terra with xhigh effort and check after every eight action chunks.
Their prompts emphasize object identity and spatial relationships.

Twenty workers dynamically claim episodes across four GPUs. Infrastructure-error
episodes are retained for a separate retry after the main queue. A separate
160-step paired pilot reached the eight-chunk verification and replanning boundary
in both branches without exceptions. Full results are pending.

After the paired evaluation and retries, the next stage will evaluate one branch
with detailed simulator information. A configurable branch count of one or two
is planned; neither capability is claimed as implemented by this record.
