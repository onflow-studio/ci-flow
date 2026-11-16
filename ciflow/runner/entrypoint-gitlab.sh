#!/usr/bin/env bash
# Runs one GitLab job, then exits. The runner loop on the host registers a
# single-use project runner and passes its token in CIFLOW_RUNNER_TOKEN; jobs
# run in their own containers on this slot's Docker daemon and never see it.
# ci-flow apply copies this into each GitLab project's runner image.
set -euo pipefail

token=${CIFLOW_RUNNER_TOKEN:?CIFLOW_RUNNER_TOKEN is not set}
unset CIFLOW_RUNNER_TOKEN

# The image has no docker CLI; the slot's daemon is up once its socket is.
sock=${DOCKER_HOST#unix://}
for _ in {1..60}; do
  [ -S "$sock" ] && break
  sleep 1
done
[ -S "$sock" ] || { echo "no Docker daemon at $DOCKER_HOST" >&2; exit 1; }

# The job containers run on the slot's daemon, where /var/run/docker.sock is
# that daemon's own socket: jobs that use docker reach their slot, nothing more.
exec gitlab-runner run-single \
  --url "${CIFLOW_GITLAB_URL:?}" \
  --token "$token" \
  --name "${CIFLOW_RUNNER_NAME:-ci-flow}" \
  --executor docker \
  --docker-host "$DOCKER_HOST" \
  --docker-image "${CIFLOW_JOB_IMAGE:?}" \
  --docker-volumes /var/run/docker.sock:/var/run/docker.sock \
  --builds-dir /builds \
  --cache-dir /cache \
  --max-builds 1 \
  --wait-timeout 0
