# Runners

ci-flow runs GitHub Actions jobs on this machine with ephemeral, isolated
self-hosted runners. You describe what the machine offers in its config, each
repo describes what its jobs need in its profile, and `ci-flow apply` turns
both into running runner slots.

## How a slot works

Each managed repo gets a docker compose project, `ciflow-<repo name>`, in
`~/.local/share/ci-flow/projects/` (`$CIFLOW_HOME`). In it, each slot N is:

- `dind-N`: a long-lived Docker daemon for the job's containers and service
  containers, with its own image cache. Its DNS points at public resolvers,
  because Docker's embedded resolver fails for external names once dockerd
  runs inside a container.
- `runner-N`: one fresh container per job from the repo's runner image. It
  shares dind-N's network namespace, so a service a workflow publishes on a
  fixed port (`5436:5432`) is on `localhost` for the job and never collides
  with the same port in another slot. `DOCKER_HOST` points at dind-N and
  `TESTCONTAINERS_HOST_OVERRIDE=localhost` keeps Testcontainers on that
  namespace too.

The `ci-flow-runner@<project>-<N>` user unit runs the runner loop for the slot:

1. Rebuild the runner image if it is missing or more than a day old (one
   build per project at a time), so the preinstalled runner rarely needs to
   self-update at the start of a job. The rebuild uses the Dockerfile from the
   last `ci-flow apply`; it doesn't fetch the repo's profile again, so the image
   only changes in ways you have reviewed. `ci-flow status` says when the
   profile on the default branch has changed since.
2. Start dind-N and remove whatever the previous job left in it: containers,
   networks, volumes, images older than a week.
3. Ask GitHub for a single-use JIT runner config with the slot's labels. The
   GitHub token stays on the host; only the JIT config enters the container,
   where every job step has sudo. The runner is named
   `<host>-<project without ciflow->-<N>-<time>`, e.g.
   `ci-box-web-2-1791995614`, so slots of different repos never share a name;
   the dashboards read the slot number from the end (older
   `<host>-<N>-<time>` names too).
4. Run exactly one job in a fresh runner container, then loop.

Stopping the unit stops the runner container and deregisters the runner, so
no offline runners linger on GitHub. While GitHub still counts a stopped
runner as busy the deregistration fails, so the loop retries it for up to a
minute; GitHub re-queues the interrupted job. A failure to start dind or to
get a JIT config backs off up to five minutes.

The units run the installed copy of ci-flow (`lib/current`, see the README),
never a checkout. When `ci-flow install` or `apply` installs a new version, each
loop notices between jobs, exits, and systemd starts it on the new code, so an
upgrade never interrupts a job.

`ci-flow remove` of the last managed repo also removes the unit template, the
empty projects directory and the build lock.

## Machine config

`~/.config/ci-flow/config.json` (`$CIFLOW_CONFIG`):

```json
{
  "repos": [
    {
      "repo": "acme/web",
      "slots": 3,
      "labels": ["acme", "general"],
      "memory": "3584m",
      "cpus": 4,
      "node_heap": 3072,
      "dind_memory": "2g",
      "dind_cpus": 2,
      "slot": {
        "1": {"labels": ["acme", "build"], "memory": "8g", "cpus": 8, "node_heap": 6144}
      }
    }
  ],
  "fleets": []
}
```

| Key | Default | |
|---|---|---|
| `repo` | required | `owner/name` |
| `slots` | `1` | how many jobs of this repo can run at once (1–32) |
| `labels` | `[]` | labels every slot gets on top of `self-hosted, linux, x64` |
| `memory`, `cpus` | `"4g"`, `4` | limits of the runner container |
| `node_heap` | from the profile | MiB for `NODE_OPTIONS=--max-old-space-size`. V8 sizes its default heap from the container limit (about half), which is too small for big `tsc` or Next builds |
| `dind_memory`, `dind_cpus` | `"2g"`, `2` | limits of the slot's Docker daemon |
| `slot` | | per-slot overrides of `labels`, `memory`, `cpus`, `node_heap`, `dind_memory`, `dind_cpus`. `labels` here replaces the repo's labels for that slot |
| `project` | `ciflow-<name>` | compose project name, when two repos share a name; must start with `ciflow-` |
| `profile` | the repo's file | the repo profile inline, same keys as `.github/ci-flow.toml` (see below); when set, the repo's file is not read |

GitHub's queue is the scheduler: a job waits until a slot whose labels it asks
for is free. To give heavy jobs their own slot, label one slot (`build`
above) and send those jobs there with `runs-on: [self-hosted, build]`; give
the others a label the heavy jobs don't use (`general`).

`"fleets"` lists runners ci-flow doesn't manage; the dashboards watch them
the same way.

## Repo profile

`.github/ci-flow.toml` in the repo, read from its **default branch** through
the GitHub API at `ci-flow apply`. A pull request can't change it, so a PR
can't change what gets installed in the runner image. Without the file,
the image is the plain runner image plus `xz-utils` and `zstd`.

```toml
# What this repo's self-hosted jobs need on a ci-flow machine.
apt = ["postgresql-client", "fonts-liberation"]  # Debian packages baked into the image
tools = ["docker-compose"]                       # extras ci-flow knows how to install
caches = ["pnpm", "playwright"]                  # kept between jobs, shared by the repo's slots
node_heap = 3072                                 # MiB; the machine config can override per slot
```

| Key | Values |
|---|---|
| `apt` | package names |
| `tools` | `docker-compose` (the compose plugin; the runner image's docker CLI lacks it) |
| `caches` | `npm`, `pnpm`, `yarn`, `playwright`, `pip`, `uv`, `cypress` |
| `node_heap` | MiB |

Unknown keys and values are ignored with a warning. Profile changes take
effect on the next `ci-flow apply`, which rebuilds the image when the
Dockerfile changes. `ci-flow apply --profile owner/repo=path.toml` previews a
profile that isn't on the default branch yet.

### Inline profile

The same profile can live in the machine config instead, as `"profile"` on
the managed repo:

```json
{"repo": "acme/web", "slots": 2,
 "profile": {"apt": ["postgresql-client"], "tools": ["docker-compose"], "caches": ["npm"], "node_heap": 3072}}
```

It is checked by the same rules, except that a bad entry is a config error
instead of a warning: you wrote it, so ci-flow refuses rather than guessing.
Use it when adding a file to the repo's default branch is costly, for
example when every push to it deploys.

Which profile is used, first match wins:

1. `--profile owner/repo=path.toml` on `ci-flow apply` (a preview);
2. `"profile"` in the machine config;
3. `.github/ci-flow.toml` on the repo's default branch;
4. the defaults.

`ci-flow apply` (also with `--dry-run`) and `ci-flow status` print which one
it was.

## Security

A self-hosted runner runs whatever the workflow says, on your machine.
GitHub advises against self-hosted runners on public repos, because a pull
request from a fork can run code here. A fresh container per job limits what
a job can leave behind, not what it can do while it runs (your network, your
CPU).

`ci-flow add` and `ci-flow apply` warn when:

- the repo is public;
- a public repo doesn't require approval for fork pull requests from all
  external contributors (Settings → Actions → General);
- a private repo runs workflows from fork pull requests without approval.

They warn rather than refuse: the decision is yours.

Workflows can also keep fork pull requests off the self-hosted runner by
themselves. ci-flow's own tests do (`.github/workflows/test.yml`):

```yaml
if: github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository
runs-on: [self-hosted, ci-flow]
```

Pushes and pull requests from branches of the repo run; a pull request from a
fork skips the job, so its code never reaches the machine.

## ci-flow tests itself on ci-flow

The unit tests run on every pull request and push to main, on a ci-flow slot
of this repo labelled `ci-flow`, with the fork gate above. The machine that
runs them has:

```json
{"repo": "onflow-studio/ci-flow", "slots": 1, "labels": ["ci-flow"], "memory": "1g", "cpus": 1}
```

`actions/setup-python` installs each Python version of the matrix in the job.

## Self-test

`.github/workflows/selftest.yml` checks a machine end to end: the job runs in
its slot's own Docker daemon, a service on a fixed port is on `localhost`, and
the workspace starts empty. Give this repo a slot with the `ci-flow-selftest`
label and run it. On a machine that already has this repo's `ci-flow` slot,
add the label to that entry (a repo is listed once); otherwise:

```json
{"repo": "onflow-studio/ci-flow", "slots": 1, "labels": ["ci-flow-selftest"], "memory": "2g", "cpus": 2}
```

```sh
ci-flow apply
gh workflow run selftest.yml -R onflow-studio/ci-flow   # or push the ci-flow-selftest branch
```

Take the label off (or `ci-flow remove onflow-studio/ci-flow --purge` for a
throwaway slot) and `ci-flow apply` again afterwards.
