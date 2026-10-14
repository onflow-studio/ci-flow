# Going public

The files in this repository are ready to publish: MIT license, a README for
newcomers, SECURITY.md and CONTRIBUTING.md, and no private names, hostnames or
paths in the tree. Some things can't be cleaned in place, though.

## What stays private in this repository

- **Git history.** Earlier commits contain the first machine's hostname,
  the private and client repository names it served, and its tailnet name, in
  code, docs and commit messages.
- **Pull request refs.** GitHub keeps every pull request's commits under
  `refs/pull/<n>/head`, even after a history rewrite and force-push. Removing
  them needs GitHub Support.
- **Issues and pull request text.** Issue bodies, PR descriptions and comments
  name client repositories, run URLs and the machine.
- **Actions history.** Workflow run logs and artifacts from the self-test and
  test runs show runner names with the hostname.

Rewriting history here would not remove the pull request refs or the issue
text, so making this repository public would publish all of it.

## Recommended: publish a fresh repository

Push the scrubbed tree as a single initial commit to a new public repository,
and keep this one private as the working history. From an up-to-date `main`:

```sh
# Final check: nothing private in the tree.
git grep -niE '<your private names, hostnames, tailnet>' && echo "fix these first"

# A new public repo with one commit.
tmp=$(mktemp -d)
git archive main | tar -x -C "$tmp"
cd "$tmp"
git init -b main
git add -A
git commit -m "ci-flow: run GitHub Actions on a spare machine and watch it from anywhere"
gh repo create onflow-studio/ci-flow-public --public --source . --push \
  --description "Run GitHub Actions on a spare machine and watch it from anywhere" \
  --homepage https://onflow.studio/ci-flow
```

Choose the final name first. If the public repository should be
`onflow-studio/ci-flow`, rename this one first
(`gh repo rename ci-flow-private -R onflow-studio/ci-flow`), then create the
public one under the freed name. Machines that cloned the private repo need
`git remote set-url origin …` afterwards.

Then, on the public repository:

1. **Enable private vulnerability reporting** (Settings → Code security), which
   SECURITY.md points to.
2. **Require approval for fork pull requests from all outside contributors**
   (Settings → Actions → General).
3. **Register its test runner.** The test workflow runs on `[self-hosted,
   ci-flow]`; add the new repository to a machine's config
   (`ci-flow add onflow-studio/<name> --runners 1 --labels ci-flow`, small
   limits) and `ci-flow apply`. `ci-flow apply` will warn that the repository
   is public; the workflow's fork gate keeps fork pull requests off the runner.
4. **Issues.** Open the remaining roadmap items (several machines, …) again in
   the public repository, written without private details.
5. Update links that point at this repository (onflow.studio, the badge in the
   README if the name changed).
