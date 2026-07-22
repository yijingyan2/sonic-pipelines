# Submodule Pipelines Trigger

`submodule-pipelines-trigger.yml` validates the SONiC submodule build pipelines in
**dependency-level order** against the exact submodule commits pinned by
[`sonic-buildimage`](https://github.com/sonic-net/sonic-buildimage), and — if every level
passes — opens a "validated" pull request back to `sonic-buildimage`.

It is a manual / scheduled pipeline (`trigger: none`, `pr: none`); it is not triggered by
commits.

## What it does

1. **Resolve the buildimage commit.** Finds the latest *successful* run of the
   `sonic-buildimage` official build pipeline (`VSbBuildPipelineId`, default `142`) on
   `master` and reads the commit (`sourceVersion`) it built.
2. **Trigger submodules, level by level.** For each dependency level, in order, it resolves
   every submodule's pinned SHA at that buildimage commit and queues the matching build
   pipeline. A level must fully succeed before the next level starts.
3. **Retry on failure.** If a triggered run fails, it retries the *failed stages in the same
   run* (via the Azure DevOps REST timeline + stage `retry` API) once before giving up.
4. **Open a validation PR.** After all levels pass, the `CreatePR` stage collects the
   validated commits, writes them to a tracking file, and opens a PR to
   `sonic-net/sonic-buildimage` (from the `mssonicbld` fork) with the **`automerge`** label.

## Stage flow

```
ResolveCommit
   └─> level0 ─> level1 ─> level2 ─> level3 ─> CreatePR
```

Each `levelN` stage depends on `ResolveCommit` (to read the buildimage SHA output) **and**
on the previous level (to enforce ordering). `CreatePR` depends on `ResolveCommit` and all
levels, and runs only on `succeeded()`.

## Dependency levels

Levels encode build order — a submodule is placed in a level after everything it depends on.
Within a level, all submodule pipelines run **in parallel** (one job each).

| Level | Submodules (path → pipeline id) |
|-------|----------------------------------|
| level0 | `common_libs` (465), `src/sonic-dash-api` (1318) |
| level1 | `platform/vpp` (1016), `src/sonic-swss-common` (9), `src/sonic-platform-common` (42), `src/sonic-platform-daemons` (41), `src/sonic-host-services` (935), `src/sonic-mgmt-framework` (130), `src/sonic-mgmt-common` (127), `src/sonic-snmpagent` (106), `src/sonic-dbsyncd` (110) |
| level2 | `src/sonic-sairedis` (12), `src/sonic-gnmi` (934), `src/sonic-utilities` (55), `src/sonic-bmp` (1565), `src/sonic-dash-ha` (2351), `src/linkmgrd` (388), `src/dhcpmon` (901), `src/dhcprelay` (487), `src/sonic-stp` (84), `src/wpasupplicant/sonic-wpa-supplicant` (5) |
| level3 | `src/sonic-swss` (15) |

> `common_libs` is special-cased: it is not a submodule gitlink, so its commit is the
> buildimage commit itself (`git rev-parse HEAD`). All other entries resolve their pinned
> commit via `git ls-tree <buildimageSha> <path>`.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `targetProject` | string | `build` | Azure DevOps project that hosts the submodule pipelines. |
| `VSbBuildPipelineId` | string | `142` | Pipeline id of the `sonic-buildimage` official build, used to resolve the reference commit. |
| `levels` | object | see above | Ordered list of levels; each has `name`, `dependsOn` (list), and a `pipelines` map of `submodule-path: pipeline-id`. |

## How a submodule job works

For each `submodule-path: pipeline-id` in a level:

1. Clone `sonic-buildimage`, check out the resolved buildimage SHA, and read the submodule's
   pinned commit. A missing pinned commit is treated as a **configuration error** and fails
   the job (wrong path or a removed/renamed submodule).
2. Queue the submodule pipeline at that commit
   (`az pipelines run --id <pipeline-id> --commit-id <sha> --branch refs/heads/master`).
3. Poll run status every 600s until `completed`; treat `succeeded` **or**
   `partiallySucceeded` as pass.
4. On failure, retry the failed stages in the same run once, then wait for the retried
   attempt to finish.
5. Publish the validated `path=sha` record as a pipeline artifact
   (`commits_<level>_<safe-path>`) for the `CreatePR` stage.

Each job has `timeoutInMinutes: 540` (9h) — this is the ceiling for a single submodule
build plus queue/poll time.

## Requirements

The pipeline expects the following to already exist in the Azure DevOps org / target project:

- **Service connection** `mssonic-automation-umi` (a User-Assigned Managed Identity) used by
  every `AzureCLI@2` task. It authenticates to Azure DevOps via an AAD access token for the
  Azure DevOps resource (`499b84ac-1321-427f-aa17-267ca6975798`).
- **Variable group** `sonicbld` containing `GITHUB-TOKEN` — a GitHub token for the
  `mssonicbld` bot with push access to the `mssonicbld/sonic-buildimage` fork and permission
  to open PRs against `sonic-net/sonic-buildimage`.
- The **`mssonicbld/sonic-buildimage`** fork (PR head branch is pushed there).
- The **`automerge`** label must exist in `sonic-net/sonic-buildimage`.
- Agent pool **`sonic-ubuntu-1c`** with enough parallelism to cover the widest level
  (level1/level2 each fan out to ~10 concurrent jobs, and each job holds an agent for the
  duration of the downstream build).

## Running it

Trigger the pipeline manually (or on a schedule) from Azure DevOps. To override a default,
supply the parameter at queue time — e.g. point at a different reference build with
`VSbBuildPipelineId`, or target a different project with `targetProject`.

## Adding or reordering a submodule

Edit the `levels` parameter default:

- Add the `submodule-path: pipeline-id` entry to the level whose dependencies it satisfies.
- If it depends on something in a later level, move it (or that dependency) so the ordering
  holds — everything a submodule needs must be in an earlier level.
- The `submodule-path` must match the path as pinned in `sonic-buildimage`
  (what `git ls-tree` reports); a wrong path fails the job by design.

## The validation PR

`CreatePR` assembles all published `path=sha` records, commits them to a
`validated-submodule-commits` tracking file on branch `validated-submodules` in the
`mssonicbld` fork, and opens (or updates) a PR titled
`[validated] Submodule pipeline runs passed for <buildimageSha>` with the `automerge` label.
If a PR for the branch already exists, the branch is force-pushed and the existing PR is
re-labeled instead of failing.
