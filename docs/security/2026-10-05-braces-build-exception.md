# Temporary build-only exception: GHSA-vfj7-8cjw-p6xm

Owner approved on 2026-10-05 after disclosure of the release audit blocker.
Applies only to ODIN 1.9.17 and expires 2026-10-19T00:00:00Z.

The advisory concerns stack exhaustion from deeply nested brace patterns in
braces <=3.0.3. At acceptance there is no patched braces release. Tailwind
3.4.19 imports braces via chokidar 3.6.0 and micromatch 4.0.8 / fast-glob 3.3.3.
These five packages are development-only entries in the reviewed lockfile.
The frontend build uses repository-owned fixed content globs, not student input.
The final Docker stage copies compiled dist assets, not node_modules. A build
hook fails if any of these packages enters the browser module graph.

This is acceptance of a narrowly bounded build-time denial-of-service risk,
not remediation of the dependency and not a vulnerability-free result.
The complete audit runs in a configuration-isolated directory with explicit
development/optional/peer inclusion. Any new finding or exposure change blocks.
The wrapper binds the frontend source, lockfile, Dockerfile and release version.
The raw report and explicit decision are retained and hashed in release evidence.
Evidence verification and every publication tag stage enforce expiry, including
recovery. No latest promotion or school deployment is authorized by this policy.

Before another release or expiry, remove the vulnerable chain, adopt an upstream
fix when available, or obtain a new separately reviewed risk decision. Never
extend the date or broaden the graph just to make a failing build pass.

Reference: https://github.com/advisories/GHSA-vfj7-8cjw-p6xm
