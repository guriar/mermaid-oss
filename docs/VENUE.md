# The venue

A venue is the stand-in a slice drives on. A slice is finished only after a full drive of
every item on one. Nothing else counts as a drive: not a shared dev tier, not a browser tab
another session can reach, not a database somebody else seeded.

`hb venue up|down|list|sweep|seats`. The verb owns identity and lifecycle only. Everything
the venue stands on is built by the product's own commands under `venue` in the config
(`docs/config.md`); the verb runs them and records what each one answered.

## Who builds one

The runner, for a run's own venue; `--owner` is recorded on the venue. Each venue has its own
browser folder, so two drives never share a login.

## Its id

`v-<issue>-<attempt>-<6 hex>`, for example `v-2264-1-9af3c1`. The issue and the attempt are
the identity; the hex keeps two venues for the same issue apart in the file system. Every
receipt a drive writes carries `venue` set to this id. The delivery ledger will refuse a row
without one.

## What `up` guarantees

- State built fresh, never inherited. A venue that inherited the previous run's state makes
  a seeded fixture unfalsifiable.
- UTC for every process it starts. A gate on the machine clock cannot see the class of
  defect that only exists in the hours after UTC midnight, so the machine clock is never the
  venue's clock.
- Every port the receipt reports derived from the issue number, by the one command that owns
  the convention. A half-moved stack does not error, it talks to a neighbour's.
- A browser profile directory of its own, under the venue's folder, with its own storage
  state. No two venues share one.
- A tenant provisioned by slug, and the slug is the venue id (the receipt's `tenant_id`).
- A session for that tenant's user, from `venue.sign_in`, written beside the venue and never
  into the record, so a record can be pasted into a report.
- The model is the mock unless `--real-llm`. The record says which, and a receipt claiming
  model judgment is UNMEASURED unless it says `real`.

The legs run in dependency order: ports before anything binds, the tenant after the service
it is created through, the session after the user the tenant mints. A leg that cannot run on this host is recorded `unmeasured` with its
reason and the venue comes back `incomplete` with a non-zero exit, never quietly green.

## What `down` guarantees

- Resumable. Each step is written to the record as it completes, so a crash mid-down is
  finished by the next `down` instead of leaving half a venue nobody can name.
- Idempotent. A second `down` on a venue already down answers `already-down` and exits 0.
- The database is dropped, unless `--keep-db`, which stops the venue and keeps its rows so a
  failed drive's evidence can still be read.

## `list`

Every venue on the host: id, owner, age, ports, model, and pid
liveness measured from the host's process table. When that table cannot be read, liveness
reads UNMEASURED, never dead.

## `sweep`

Orphans, dry run unless `--apply`: a venue older than `--older-than-hours` (12 by default),
or one serving a worktree whose branch landed (`--landed`, named by the caller).

## Collision

- A second `up` for the same issue and attempt returns the existing venue. It never builds a
  rival on the same ports.
- A port already bound by a foreign process refuses, naming the port, its role, and the pid
  holding it.
- Ports that cannot be derived, or a host whose listening ports cannot be read, refuse too.
  Not knowing is not the same as free.

## What a receipt must carry

`venue` = the id, and with it the model the venue ran and the clock it ran on. A receipt that
does not name its venue cannot be told from a receipt about somebody else's.
