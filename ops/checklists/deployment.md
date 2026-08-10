# IIC-Forge release deployment checklist

- [ ] Record release commit, Git tree, image ID/digest, Redis digest, `uv.lock`
      SHA-256, Python artifact hashes, and both SBOM hashes.
- [ ] Require all four hosted CI jobs green on that exact commit.
- [ ] Review dependency, secret/configuration, and image scan reports; confirm
      no unresolved high/critical or operator-classified P0/P1 issue.
- [ ] Require the disposable automated fault drill to pass.
- [ ] Require live private Telegram/email, provider-outage, real Beijing quiet
      boundary, and budget-boundary evidence.
- [ ] Require a verified local backup no more than one hour old.
- [ ] Require the disposable full-volume restore under four hours and the
      25-hour backup schedule observation.
- [ ] Require the 72-hour soak summary to be `passed`.
- [ ] Confirm production uses the exact reviewed `.env.production`, nine
      mode-0600 secret files, and a digest-pinned `IIC_IMAGE` or verified local
      image ID.
- [ ] Confirm only RSS, Telegram, and Polygon ingestion are enabled.
- [ ] Confirm Redis/dashboard have no public binding and dashboard listens only
      on `127.0.0.1` through the private operator path.
- [ ] Record the previous image digest and current backup as rollback points.
- [ ] Stop writers, drain active analysis, run strict preflight, deploy the
      candidate, then start one-shot initialization before long-running services.
- [ ] Re-run strict preflight, verify all services healthy, inspect open
      operational alerts, and send one private post-deploy Telegram/email probe.
- [ ] Observe queues, restarts, disk, and backup freshness for at least one hour
      after deployment before closing the release window.
