# Releasing

How a Tellybox release is made. Releases are the signal for self-hosters to update, so they are cut on purpose and never by accident: a release only happens when the owner pushes a `v*` tag.

## Versioning

[Semantic versioning](https://semver.org/): `MAJOR.MINOR.PATCH`. While the major version is 0, a minor bump (`0.1` to `0.2`) may contain breaking changes, and a patch bump (`0.1.0` to `0.1.1`) only fixes bugs. The single source of the version is `version` in `pyproject.toml`.

## Before you start

- `main` is green (the *Test, build and deploy* workflow passed on the commit you want to release).
- `docs/PROGRESS.md` is up to date.
- Anything a user must do when upgrading (a manual step, a changed setting) is in `docs/installation.md`. Database migrations run on their own, but they only move forward.

## Steps

1. Bump `version` in `pyproject.toml` on a branch and merge it by pull request.
2. Update your checkout to the merge commit on `main`.
3. Tag that commit and push the tag:

   ```sh
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Tellybox X.Y.Z"
   git push origin vX.Y.Z
   ```

The tag must be `v` plus the exact `version` from `pyproject.toml`. The workflow fails right away if they differ, before anything is built or published.

## What CI does

Pushing a `v*` tag runs `.github/workflows/docker-publish.yml`:

1. **test:** checks the tag against `pyproject.toml`, lints `deploy/install.sh` and runs `pytest -q`.
2. **build-and-push:** builds the image for `linux/amd64` and `linux/arm64` and pushes it to `ghcr.io/sandermvanvliet/tellybox` as `X.Y.Z`, `X.Y`, `latest` and `sha-<commit>`. `X.Y.Z` is also baked into the image as its version (`TELLYBOX_VERSION`).
3. **release:** creates the GitHub release (only if one doesn't exist yet) with notes generated from the merged pull requests, grouped by label as set in `.github/release.yml`, and attaches `docker-compose.yml`, `env.example` and `install.sh` from `deploy/`.

Releases are never deployed by this workflow. Only pushes to `main` build `edge` and deploy.

## Check the result

- The workflow run for the tag is green in the Actions tab.
- `gh release view vX.Y.Z` shows the release with the three files attached.
- Both architectures are in the image:

  ```sh
  docker pull ghcr.io/sandermvanvliet/tellybox:X.Y.Z
  docker buildx imagetools inspect ghcr.io/sandermvanvliet/tellybox:X.Y.Z
  ```

## Editing the notes

The generated notes are a starting point. Edit them on the release page (or with `gh release edit vX.Y.Z --notes-file notes.md`) to add a sentence on anything that needs attention when upgrading. Labelling pull requests (`enhancement`, `bug`, `documentation`, `dependencies`) before merging sorts them into the right group; pull requests labelled `ignore-for-release` and Dependabot's are left out.

## If a release is bad

Never move or delete a tag that people may have pulled. Fix the problem on `main` and release the next patch version (`X.Y.Z+1`); `latest` and `X.Y` then move forward. Delete a release only if it was never used, for example when the workflow failed halfway, and then delete its tag too so the version can be tagged again.

## How others follow releases

- On GitHub, **Watch**, then **Custom**, then **Releases** sends a notification for each new release.
- Dependabot or Renovate can watch the image tag in your compose file and open a pull request for each new version. This needs a pinned tag, not `latest`; see "Following releases" in the [installation guide](installation.md#updating).
- `TELLYBOX_TAG` in `.env` pins the image to a version (`0.1.0`) or to a minor version that gets patch fixes (`0.1`).
