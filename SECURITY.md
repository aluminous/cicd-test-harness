# Security policy

## Preview support

The `0.1.x` line is an alpha preview. Security fixes will be made on the latest preview
release; older preview versions are not maintained separately.

Please report suspected vulnerabilities privately through the hosting platform's security
advisory mechanism. If private advisories are unavailable, contact the repository owner
privately before opening a public issue.

## Important trust boundaries

The supplied profiles create disposable test clusters, not hardened shared environments.
They intentionally use fixed test credentials, disable some authentication controls, and
grant broad Clouddriver permissions inside the disposable Kind cluster. Do not deploy
these manifests to production, a shared Kubernetes cluster, or a network-accessible host.

Host endpoint exposure binds to `127.0.0.1` by default. Registry credentials are read
from named environment variables, written to private temporary auth files, redacted from
diagnostics, and removed during teardown. Kubernetes Secrets are not collected.

Reports involving dependency or container-image vulnerabilities should identify the
affected profile, component version, image digest when available, and whether the issue
is reachable in the reduced harness configuration.
