# Education upload storage reserve

By default ODIN leaves the greater of **10 GiB or 10% of the upload filesystem's total capacity** after each streamed chunk. Administrator environment variable `EDUCATION_MIN_FREE_GIB` overrides only the absolute minimum. It cannot disable the 10% requirement.

Example for a limited POC:

```yaml
services:
  odin:
    environment:
      EDUCATION_MIN_FREE_GIB: "1"
```

Merge this entry into the existing container configuration, retaining all existing environment variables, volumes and encryption keys. Recreate the ODIN container using the installation's usual Compose procedure to apply the environment change. A process restart alone does not change an existing container's environment. Remove the variable and recreate to restore the 10 GiB default. No database migration is needed.

The value must be a finite number at least 1 GiB and below 8589934592 GiB; fractions are supported and rounded up to a whole byte. Empty, malformed, zero, negative, nonfinite or overflowing values block uploads with a configuration error. There is no permissive fallback.

The check uses `EDUCATION_UPLOAD_ROOT`, default `/data/education_uploads`, not a hardcoded VM root disk. Confirm the intended volume is mounted there. On a 16 GiB filesystem, a configured 1 GiB minimum results in approximately 1.6 GiB effective reserve. The percentage applies to total filesystem capacity, not current free space. Human-readable `df` values are rounded.

POC readiness displays the administrator minimum, effective reserve and measured headroom. If the upload directory is not yet created, it labels an existing-parent filesystem measurement. Readiness does not verify mount intent, write permissions, account quotas, file compatibility or physical printing.

100 MiB compressed and 500 MiB uncompressed file limits, user/tenant quotas, upload rate limits and per-chunk rejection/cleanup remain enforced. The disk check is a snapshot, not an atomic filesystem reservation. Concurrent uploads, unrelated writers, database/WAL growth, logs and backups can consume space. Administrators must choose a margin suited to the actual workload; a small POC reserve is not a long-term sizing recommendation.
