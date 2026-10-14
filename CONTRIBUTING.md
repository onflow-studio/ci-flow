# Contributing

Thanks for helping. A few things to know:

- **Python standard library only.** No dependencies, no build step. The web
  dashboard is one static HTML file.
- **Tests:** `python3 -m unittest discover -s tests`. They must pass without
  Docker, systemd, gh or Tailscale on the machine; stub what you need.
- **CI:** ci-flow tests itself on a ci-flow runner. Pull requests from forks
  skip that job (the workflow's fork gate), so please run the tests locally;
  after review, a maintainer runs them by pushing your branch to this
  repository.
- **Live machines:** anything that touches runners, units or Docker must never
  kill a running job and must only touch ci-flow's own `ciflow-*` resources.
- **Docs:** user-facing changes update the README or `docs/`.

Open an issue before large changes, so we can agree on the approach.
