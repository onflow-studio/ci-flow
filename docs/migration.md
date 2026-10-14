# Migrating hand-built runner fleets

Many self-hosted setups start as a copied folder per project: a compose file,
a runner loop script and a systemd unit. This guide moves such fleets onto
ci-flow managed repos, one slot at a time, without killing a running job.

The example uses two made-up fleets on one machine:

| Fleet | Repo | Runners | Labels | Runner limits | dind | Image extras | Caches |
|---|---|---|---|---|---|---|---|
| `web-ci` | acme/web | 1 | web, build | 8g / 8 cpu, heap 6144 | 2560m / 2 | postgresql-client | pnpm store, Playwright |
| | | 2–4 | web, general | 3584m / 4, heap 3072 | 2560m / 2 | | |
| `apps-ci` | acme/crm | 1–2 | ci-box | 4g / 4, heap 3072 | 2g / 2 | postgresql-client, fonts-liberation, compose plugin | npm, Playwright |
| | acme/portal | 3–4 | ci-box | 4g / 4, heap 3072 | 2g / 2 | same | same |

## 1. Write the equivalent config

```json
{
  "repos": [
    {
      "repo": "acme/web",
      "slots": 4,
      "labels": ["web", "general"],
      "memory": "3584m", "cpus": 4, "node_heap": 3072,
      "dind_memory": "2560m", "dind_cpus": 2,
      "slot": {"1": {"labels": ["web", "build"], "memory": "8g", "cpus": 8, "node_heap": 6144}},
      "profile": {"apt": ["postgresql-client"], "caches": ["pnpm", "playwright"], "node_heap": 3072}
    },
    {
      "repo": "acme/crm", "slots": 2, "labels": ["ci-box"], "memory": "4g", "cpus": 4, "node_heap": 3072,
      "profile": {"apt": ["postgresql-client", "fonts-liberation"], "tools": ["docker-compose"],
                  "caches": ["npm", "playwright"], "node_heap": 3072}
    },
    {
      "repo": "acme/portal", "slots": 2, "labels": ["ci-box"], "memory": "4g", "cpus": 4, "node_heap": 3072,
      "profile": {"apt": ["postgresql-client", "fonts-liberation"], "tools": ["docker-compose"],
                  "caches": ["npm", "playwright"], "node_heap": 3072}
    }
  ]
}
```

Keep the labels your workflows' `runs-on` already ask for, so no workflow has
to change.

Put the profiles inline (`"profile"`, see
[runners.md](runners.md#inline-profile)) rather than as `.github/ci-flow.toml`
files in the repos: a push to a repo's default branch usually runs its
workflows, production deploys included. A repo file can come later, through a
reviewed PR whose merge commit carries `[skip ci]`.

## 2. Compare with a dry run

`ci-flow apply --dry-run` prints the compose projects, Dockerfiles and units
it would write. Compare them with `docker compose config` of the old fleets:
memory and CPU limits, environment (`DOCKER_HOST`,
`TESTCONTAINERS_HOST_OVERRIDE`, `NODE_OPTIONS`), mounts, the dind command,
DNS, privileges, runner labels and image packages should all match.

What changes on purpose:

- **Names.** Compose projects become `ciflow-<repo>` and units
  `ci-flow-runner@<project>-<slot>`. New projects mean new volumes, so the
  first jobs after the switch start with cold image and package caches.
- **Slot numbers** are per repo: `acme/portal`'s runners 3 and 4 become its
  slots 1 and 2.
- **Caches** are kept per repo, where a hand-built fleet may share one cache
  volume between repos.

## 3. Move one idle slot at a time

Do one fleet at a time, starting with the one where a mistake costs least.
For each old runner:

1. Wait until it's idle: no in-progress job on GitHub with its runner name
   (`cimon` shows it idle).
2. Stop it: `systemctl --user disable --now <old-unit>@N`. An idle runner
   deregisters on stop.
3. Raise the repo's `slots` by one in the config and run `ci-flow apply`.
   Check the new runner is online on GitHub with the same labels.

This keeps the total number of runners steady, so the machine never runs
more jobs than before.

## 4. Prove it, then clean up

Let the new slots run a few green jobs: wait for real ones, or re-run a
recent pull request CI run (not a deploy). Then remove the old fleet:

```sh
tar czf ~/old-fleet-backup.tar.gz ~/.local/share/<old-fleet> ~/.config/systemd/user/<old-unit>@.service
docker compose -p <old-fleet> down --volumes
docker image rm <old-fleet>-runner:latest
rm ~/.config/systemd/user/<old-unit>@.service && systemctl --user daemon-reload
```

Revoke any access token the old fleet kept in its own env file.

If a project kept its runner files in its repository, replace them with a
short README pointing to the machine's ci-flow setup, in a PR merged with
`[skip ci]`; notes about which jobs run self-hosted belong to that project.

## Rollback

At any step: `ci-flow remove <repo>`, restore the config backup and enable
the old units again.
