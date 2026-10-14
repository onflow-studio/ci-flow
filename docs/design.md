# Design

ci-flow turns a spare machine into GitHub Actions compute: ephemeral,
isolated self-hosted runners, reachable only over a Tailscale tailnet, with a
dashboard in the terminal and the browser. GitHub stays the CI system
(workflows, queue, logs); ci-flow is the machine underneath.

## Scope

- GitHub Actions only.
- One machine for now; several machines are next (see the roadmap).
- Open source. Users install it on their own machine.

## Runners

ci-flow owns the runner engine. Each runner slot N is a pair:

- `dind-N`: a long-lived Docker daemon for job and service containers, with its
  own image cache.
- `runner-N`: a fresh container per job, created from a single-use JIT runner
  config, sharing `dind-N`'s network namespace so fixed service ports never
  collide across slots.

After each job the slot's daemon is cleaned (containers, networks, volumes,
old images) and the runner image is rebuilt at most daily.

`ci-flow apply` renders one compose project per managed repo into
`~/.local/share/ci-flow/projects/<project>/`, builds its runner image, and
starts a `ci-flow-runner@<project>-<slot>` user unit per slot. One generic
runner loop (`ciflow/loop.py`) serves every slot; nothing is copied per repo.
See [runners.md](runners.md).

Everything that runs as a unit runs the installed copy in
`~/.local/share/ci-flow/lib/current`, never a git checkout, so a branch switch
can't change live code. Installing a new version restarts the collector and
lets each runner slot finish its job before it switches. The collector rereads
the config when it changes, so config edits need no restart.

## Configuration

Split by who decides what:

| Where | Decides |
|---|---|
| Repo: `.github/workflows/*.yml` | What runs. Jobs opt in with `runs-on: [self-hosted, <label>]`. |
| Repo: `.github/ci-flow.toml` | What the jobs need: extra packages, caches to keep, Node heap. Read from the default branch only, never from a PR. TOML because Python reads it with the standard library and it allows comments. |
| Machine: `~/.config/ci-flow/config.json` | What the machine offers: which repos it serves, how many slots each, labels per slot, memory and CPU limits. |

## Monitoring

One collector daemon, `ci-flowd`, polls the machine, docker, systemd and the
GitHub API and keeps a single snapshot. It serves it on 127.0.0.1:7340 as JSON
(`/api/snapshot`) and as server-sent events (`/api/events`). Views read that
snapshot instead of polling GitHub themselves:

- `cimon`: the terminal dashboard. Without a running collector it collects
  in-process.
- A web page served on the tailnet with `tailscale serve`, using the same
  layout. Tailscale provides HTTPS and the viewer's identity; there is no
  separate login. Plain Python and one static HTML file, no build step.

The rules for what the dashboards show (busy slots, running vs queued runs,
the health badge, recent results) live once in `ciflow/model.py`. The
collector adds their result to each snapshot as `view`, so the web page only
draws it.

## Access

Nothing is exposed publicly. Dashboard and SSH go over the tailnet.

## Security

GitHub advises against self-hosted runners on public repositories, because a
fork's pull request can run code on the machine. Ephemeral runners limit what a
job can leave behind but not what it can do while running (network, CPU).
ci-flow checks that fork pull requests require approval and warns when a public
repository is added.

## Roadmap

Done: the collector with the terminal and web dashboards, the runner engine
with repo profiles, and `ci-flow init` for a fresh machine (Docker, Tailscale,
gh, the collector, laptop care: lid switch, Docker prune, alerts).

Next: several machines on one tailnet behind one dashboard, with capacity-aware
slots per machine and draining a machine out of rotation.
