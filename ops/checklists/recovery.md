# IIC-Forge recovery checklist

- [ ] Identify the failure domain: one process, delivery/provider, Redis,
      SQLite/data volume, backup media, disk capacity, or whole host.
- [ ] Run bounded operator status and strict preflight; preserve their output.
- [ ] Stop writers before any Redis/SQLite repair or restore.
- [ ] For abandoned analysis/delivery leases, restart the responsible worker and
      verify fenced reclamation. Use audited retry/requeue only after correcting
      the cause.
- [ ] For Redis failure, require AOF manifest/write health; restore the paired
      data and Redis recovery point when persistence is corrupt.
- [ ] For SQLite failure, require archive authentication before downtime and use
      the guarded two-volume restore. Never copy a live SQLite file directly.
- [ ] For budget exhaustion, normally wait for Beijing midnight. Release only a
      proven abandoned reservation with provider-side evidence and the exact
      audited confirmation.
- [ ] For low disk, expand storage or review retention preview before applying;
      never recursively delete the data or backup root.
- [ ] Measure restore elapsed time and estimated data-loss interval; require RTO
      below four hours and RPO no more than one hour while local backup storage survives.
- [ ] After recovery, require SQLite integrity/foreign keys, Redis AOF health,
      all heartbeats, current connector cursors, stable queue counts, private
      Telegram/email probes, and a new verified backup.
- [ ] Host plus local-backup loss is outside the approved guarantee. Report it
      accurately; do not claim recovery without surviving media and key material.
