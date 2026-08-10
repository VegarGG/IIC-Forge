# IIC-Forge incident checklist

- [ ] Classify severity: P0 for active credential exposure, corrupt/lost state,
      uncontrolled external action, or unrecoverable service; P1 for sustained
      ingestion/delivery/analysis outage, budget bypass, or failed recovery gate.
- [ ] Record UTC and Beijing timestamps, commit/image digest, affected service,
      first symptom, and operator status snapshot. Do not record secrets or
      untrusted payload bodies.
- [ ] Contain with the smallest action: stop the affected connector/worker or
      all writers when state integrity is uncertain. Keep Redis/SQLite volumes.
- [ ] For suspected credential exposure, stop egress-capable services, rotate
      the affected provider secret outside logs/history, and invalidate sessions
      before restart.
- [ ] Preserve Docker logs, service restart counts, queue metadata, SQLite
      integrity results, Redis persistence state, and backup marker/checksum.
- [ ] Determine whether any cursor advanced without durable state, any lease
      completed late, any logical delivery duplicated, or any budget reservation
      escaped the Beijing-day ledger.
- [ ] Recover using the documented operator command or recovery checklist. Do
      not mutate SQLite tables or Redis streams manually unless a reviewed repair
      procedure explicitly requires it.
- [ ] Confirm operational alerts resolve, strict preflight passes, both delivery
      channels work, and a fresh verified backup exists.
- [ ] Record impact, root cause, data/message loss or duplication, recovery time,
      security exposure, accepted residual risk, and the preventive change.
- [ ] Keep the release blocked until every P0/P1 follow-up has an owner and is
      resolved or the candidate is replaced.
