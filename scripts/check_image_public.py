#!/usr/bin/env python
"""Is the container image anonymously pullable? If not, Fargate cannot start it.

AWS Batch on Fargate pulls the image with the ECS execution role, which has no
ghcr.io identity. A private ghcr package therefore fails at task start with a
`CannotPullContainerError`, before any of our code runs -- so this is worth
knowing before submitting a campaign rather than after.

Two ways to make a private package work; this repo takes the first, as
QuakeScope does (its `register_jobdef.py` resolves manifests through the same
anonymous token endpoint, which only succeeds for a public package):

  1. make the ghcr package PUBLIC -- no credentials anywhere, nothing to rotate
  2. keep it private and add `repositoryCredentials` to both job definitions,
     pointing at a Secrets Manager secret holding a ghcr PAT, and grant
     `secretsmanager:GetSecretValue` to the EXECUTION role (not the job role --
     on Fargate the execution role is what resolves `secrets:`)

The check uses the registry's anonymous token flow and stdlib only, so it runs
in CI, in a container, or on a laptop with no gh login and no AWS credentials.

    python scripts/check_image_public.py
    python scripts/check_image_public.py --repo noisepy/noisepy-dvv-cloud \
        --tags correlate-latest dvv-latest

Exit 0 only if every tag is anonymously pullable.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

REGISTRY = "ghcr.io"
DEFAULT_REPO = "noisepy/noisepy-dvv-cloud"
DEFAULT_TAGS = ("correlate-latest", "dvv-latest")

# Ask for every manifest media type we might get back. Omitting the OCI index
# types makes a multi-arch image answer 404 rather than 200, which would read
# as "missing" instead of "present".
ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


_FIX = """
Fix: GitHub -> Packages -> noisepy-dvv-cloud -> Package settings ->
     Change visibility -> Public. Needs admin on the package; a `gh` token
     with only `repo` scope cannot do it through the API.
     Or keep it private and wire repositoryCredentials -- see the module
     docstring for what that costs."""


def anon_token(repo: str) -> tuple[str | None, str]:
    """Pull token for an anonymous client, plus why it failed.

    ghcr is unambiguous here, which is the whole basis of this check:

      public   HTTP 200 and a {"token": ...} body
      private  HTTP 401 and {"errors":[{"code":"UNAUTHORIZED", ...}]}

    So a 401 is a definite "private", not an inconclusive result. Only a
    transport failure is genuinely unknown, and the two must not be reported
    the same way -- absence of evidence is not evidence of safety.
    """
    url = f"https://{REGISTRY}/token?scope=repository:{repo}:pull&service={REGISTRY}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            token = json.load(resp).get("token")
            return (token, "") if token else (None, "private")
    except urllib.error.HTTPError as exc:
        return (None, "private" if exc.code in (401, 403) else f"http {exc.code}")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return (None, f"unreachable: {type(exc).__name__}")


def manifest_status(repo: str, tag: str, token: str) -> int:
    req = urllib.request.Request(
        f"https://{REGISTRY}/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--tags", nargs="+", default=list(DEFAULT_TAGS))
    args = ap.parse_args(argv)

    print(f"{REGISTRY}/{args.repo} — anonymous pull check")
    token, why = anon_token(args.repo)
    if token is None and why == "private":
        for tag in args.tags:
            print(f"  {tag:<20} ---       PRIVATE — Fargate will fail with "
                  "CannotPullContainerError")
        print(_FIX)
        return 1
    if token is None:
        print(f"  UNKNOWN: no anonymous token ({why}). This is a transport "
              "problem, not a verdict on visibility.")
        return 2

    worst = 0
    for tag in args.tags:
        code = manifest_status(args.repo, tag, token)
        if code == 200:
            verdict = "PUBLIC — Fargate can pull this"
        elif code in (401, 403):
            verdict = "PRIVATE — Fargate will fail with CannotPullContainerError"
            worst = max(worst, 1)
        elif code == 404:
            verdict = "NOT FOUND — tag was never pushed, or the name is wrong"
            worst = max(worst, 1)
        else:
            verdict = "UNKNOWN"
            worst = max(worst, 2)
        print(f"  {tag:<20} HTTP {code or '---'}  {verdict}")

    if worst == 1:
        print(_FIX)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
