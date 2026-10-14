# Security

ci-flow runs other people's code on your machine, so security reports matter.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private vulnerability
reporting on this repository (Security → Report a vulnerability), with the
version (`ci-flow` prints it on install), what you found and how to
reproduce it. You'll get an answer within a week.

## What ci-flow protects, and what it doesn't

Each job runs in a fresh runner container with its own Docker daemon, which is
cleaned after the job. A job can't leave anything behind for the next job in
the same slot, and slots don't share Docker state.

While it runs, a job can still use the machine's network (including your
tailnet, unless your Tailscale ACLs prevent it), its CPU and memory within the
slot's limits, and anything a privileged Docker daemon can reach. Only run
jobs you trust:

- Keep self-hosted runners off public repositories, or make sure pull requests
  from forks can never reach them: require approval for fork pull requests
  from all outside contributors, and gate jobs with
  `if: github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository`.
  `ci-flow add` and `apply` warn when a repo is public and when its fork
  settings are risky.
- The dashboard listens on 127.0.0.1. Share it only on your tailnet
  (`tailscale serve`), never with `tailscale funnel`.

See [docs/runners.md](docs/runners.md#security) for details.
