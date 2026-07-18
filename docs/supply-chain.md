# Supply Chain Security

Every Orrery release ships with a verifiable chain of custody from source to
image (AEP-014). This page documents what is produced and how downstream
consumers verify it.

## What a release produces

| Artifact | Where | How |
|----------|-------|-----|
| Multi-arch image (amd64/arm64) | `ghcr.io/bahalla/orrery:<version>` | `docker/build-push-action` with `provenance: true` + `sbom: true` (BuildKit attestations) |
| Keyless signature | Sigstore transparency log (Rekor) | `cosign sign` against the GitHub Actions OIDC identity — no long-lived keys to rotate or leak |
| Python SBOM (CycloneDX) | `sbom-python.cdx.json` release asset | `uv export --frozen` → `cyclonedx-py`; covers the full locked Python graph, including transitive LLM-provider SDKs that OS-layer scanners miss |
| Pinned digest list | `docker-images.txt` release asset | tag@digest references for reproducible pulls |
| Vulnerability scan | GitHub Code Scanning (SARIF) | Trivy scans the pushed image; HIGH/CRITICAL fixable CVEs **fail the release** |

## Verifying an image signature

Images are signed keylessly: the signing identity is the release workflow
itself, recorded in the Sigstore transparency log. Verify with
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```bash
cosign verify \
  --certificate-identity-regexp "https://github.com/BAHALLA/orrery/.*" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/bahalla/orrery:0.2.1
```

A valid result proves the image was built by this repository's GitHub Actions
workflow — a token with GHCR write access alone cannot forge it, because it
cannot mint the workflow's OIDC identity.

## Inspecting the SBOM

Each release attaches `sbom-python.cdx.json` (CycloneDX). Feed it to any SBOM
tooling, e.g. scan it independently with Trivy or Grype:

```bash
gh release download v0.2.1 --pattern 'sbom-python.cdx.json'
trivy sbom sbom-python.cdx.json
```

The BuildKit-embedded OS-layer SBOM is also queryable straight from the
registry:

```bash
docker buildx imagetools inspect ghcr.io/bahalla/orrery:0.2.1 \
  --format '{{ json .SBOM }}'
```

## Build-time guarantees

- **Base images pinned by digest** — every `FROM` in the `Dockerfile` carries
  a `tag@sha256:...` reference, so two builds resolve the same base bytes; a
  tag hijack upstream cannot silently change what ships. Dependabot's
  `docker` ecosystem opens PRs when upstream digests move.
- **PR dependency review** — `actions/dependency-review-action` fails any PR
  that introduces a dependency with a known HIGH+ CVE.
- **Filesystem + config scanning** — every CI run Trivy-scans the repo
  (vulnerabilities, IaC misconfigurations, committed secrets) and `bandit`
  checks the Python source.

## Admission-time enforcement (optional)

Clusters running the [Sigstore Policy
Controller](https://docs.sigstore.dev/policy-controller/overview/) can refuse
to admit anything except images signed by this repository's release workflow.
Apply the shipped policy and label the namespaces to enforce:

```bash
kubectl apply -f deploy/k8s/imagepolicy.yaml
kubectl label namespace <ns> policy.sigstore.dev/include=true
```

See [`deploy/k8s/imagepolicy.yaml`](https://github.com/BAHALLA/orrery/blob/main/deploy/k8s/imagepolicy.yaml)
— it admits `ghcr.io/bahalla/orrery*` only with a valid keyless signature
from this repository's GitHub Actions identity.
