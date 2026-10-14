#!/usr/bin/env bash
# Runs one ephemeral job inside a fresh container. The runner loop on the host
# mints a single-use JIT config and passes it in RUNNER_JITCONFIG; no GitHub
# credential ever enters the container, since every job step has sudo here.
# ci-flow apply copies this into each project's runner image.
set -euo pipefail

jit=${RUNNER_JITCONFIG:?RUNNER_JITCONFIG is not set}
unset RUNNER_JITCONFIG

# Volumes come up root-owned when the dind side mounts them first.
sudo chown runner:runner /home/runner/_work /home/runner/externals-shared ${CIFLOW_CACHES:-}

# _work is a volume shared with dind (container jobs bind-mount it by path), so
# it outlives the container: drop the previous job's workspace, keep the tool
# and action caches.
find /home/runner/_work -mindepth 1 -maxdepth 1 ! -name _tool ! -name _actions \
  -exec rm -rf {} +

# Container jobs mount the runner's externals from the dind daemon's
# filesystem, which sees this volume at /home/runner/externals.
cp -a /home/runner/externals/. /home/runner/externals-shared/

for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info >/dev/null

exec /home/runner/run.sh --jitconfig "$jit"
