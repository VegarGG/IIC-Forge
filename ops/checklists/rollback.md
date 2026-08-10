# IIC-Forge rollback checklist

- [ ] Declare the rollback trigger and timestamp; preserve logs and the operator
      status snapshot before changing state.
- [ ] Stop RSS, Telegram, Polygon, triage, promoter, scheduler, action handler,
      analysis worker, and delivery worker. Do not delete volumes.
- [ ] If safe, wait for active fenced work to drain; otherwise record the active
      lease/job IDs and terminate through Compose.
- [ ] Confirm the target image supports the current schema version. Batch 9 and
      Batch 10 both use schema version 6.
- [ ] For an application-only regression, set `IIC_IMAGE` to the previous exact
      digest and start `database-init`/`ticker-seed` before the full stack.
- [ ] For persistent corruption or an incompatible schema, authenticate the
      selected pre-release backup before stopping stateful services, then follow
      `ops/restore.sh` with the exact confirmation phrase.
- [ ] Require SQLite integrity/foreign-key checks, Redis AOF health, service
      heartbeats, queue counts, and connector cursors after rollback.
- [ ] Re-send only deliberately selected blocked/dead work through audited
      operator commands; never edit queue state directly.
- [ ] Send a private Telegram/email probe and confirm no duplicate logical
      delivery was created.
- [ ] Record final image digest, restored archive checksum if any, elapsed time,
      data-loss window, and unresolved follow-up actions.
