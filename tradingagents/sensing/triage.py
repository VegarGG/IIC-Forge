"""F3 triage consumer — pulls from Redis, dedupes, scores, persists.

This module exposes:
  - ``Triage``: the per-envelope pipeline (``process_one``) and consumer loop.
  - ``main()``: systemd entry point.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Sequence

import redis.asyncio as aioredis

if TYPE_CHECKING:  # import-light: availability pulls in openai/httpx
    from tradingagents.llm_clients.availability import AvailabilityCounter

from tradingagents.persistence.store import (
    insert_event, insert_event_ticker,
)
from tradingagents.sensing.dedupe import DedupeStage1, DedupeStage2
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.salience import SalienceScorer, SalienceResult
from tradingagents.sensing.ticker_validator import TickerValidator
from tradingagents.sensing.watchlist import auto_promote


log = logging.getLogger(__name__)


@dataclass
class TriageResult:
    event_id: str
    status: str               # "triaged" | "duplicate"
    salience: Optional[float] = None
    deduped_of: Optional[str] = None
    matched_tickers: Sequence[str] = ()


def _open_cross_thread_conn(db_path: str) -> sqlite3.Connection:
    """Open a second, ``check_same_thread=False`` connection to ``db_path``.

    For sqlite objects that are created on one thread (the asyncio event-loop
    thread) but used from another (an executor / ``asyncio.to_thread`` worker).
    Callers MUST serialize access themselves — either a single worker thread
    owns the connection (DedupeStage2's max_workers=1 executor) or every
    holder of the connection wraps each use in ONE SHARED lock
    (AvailabilityCounter + DailyFallbackBudget in ``_main``; per-holder locks
    do NOT serialize cross-holder access and corrupt the C-level sqlite3
    state).  The standard connect() helper is intentionally not used here to
    avoid changing its global signature; no schema is applied (connect()
    already ran).
    """
    if not db_path:
        raise ValueError(
            "Triage requires a file-backed sqlite DB; "
            ":memory:/temp DBs cannot be shared across connections"
        )
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_ds2_conn(db_path: str) -> sqlite3.Connection:
    """Open a sqlite connection for DedupeStage2's dedicated executor thread.

    check_same_thread=False is required because the connection is created on
    the Triage constructor thread (the asyncio event-loop thread) but then used
    exclusively inside a single-thread ThreadPoolExecutor worker.  We document
    this explicitly: the executor has max_workers=1, so only one thread ever
    touches this connection at a time — the usual sqlite thread-safety concern
    (concurrent writes from multiple threads) cannot arise.  We load sqlite_vec
    ourselves so KNN queries work.
    """
    import sqlite_vec as _sqlite_vec
    ds2_conn = _open_cross_thread_conn(db_path)
    ds2_conn.enable_load_extension(True)
    _sqlite_vec.load(ds2_conn)
    ds2_conn.enable_load_extension(False)
    return ds2_conn


class Triage:
    """Owns the per-envelope pipeline and the consume loop.

    Constructed once per triage process; one instance is shared across
    all asyncio consumers.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        redis: aioredis.Redis,
        embedder,                                          # Embedder
        llm_call: Callable[[str], "str | Awaitable[str]"],
        data_dir: str,
        cosine_threshold: float = 0.92,
        window_hours: int = 24,
        fingerprint_ttl_hours: int = 72,
        salience_threshold: float = 0.7,
        confidence_threshold: float = 0.8,
        salience_cache_ttl_seconds: int = 86400,
        ttl_days: int = 7,
        availability_counter: "Optional[AvailabilityCounter]" = None,
    ) -> None:
        self._conn = conn
        self._redis = redis
        self._data_dir = data_dir
        self._ds1 = DedupeStage1(conn=conn, redis=redis,
                                  fingerprint_ttl_hours=fingerprint_ttl_hours)

        # DedupeStage2 calls embedder.embed() — a CPU-bound sentence-transformer
        # encode that takes 10-100+ ms and must not block the event loop.  We
        # dispatch all ds2 calls through a single-thread executor so that:
        #   (a) embed() runs off the event-loop thread, and
        #   (b) sqlite access is serialized (one thread owns the connection).
        # A dedicated connection opened with check_same_thread=False is needed
        # because the connection is created here (on the event-loop thread) but
        # used inside the executor worker thread.  max_workers=1 means only one
        # thread ever touches _ds2_conn, satisfying sqlite's thread-safety
        # requirement without requiring a lock.
        db_path = conn.execute(
            "PRAGMA database_list"
        ).fetchone()[2]  # (seq, name, file) → file is index 2
        self._ds2_conn = _open_ds2_conn(db_path)
        self._ds2_executor = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="triage-ds2")
        self._ds2 = DedupeStage2(conn=self._ds2_conn, embedder=embedder,
                                  cosine_threshold=cosine_threshold,
                                  window_hours=window_hours)

        self._scorer = SalienceScorer(redis=redis, llm_call=llm_call,
                                       cache_ttl_seconds=salience_cache_ttl_seconds)
        self._validator = TickerValidator(conn=conn)
        self._salience_threshold = salience_threshold
        self._confidence_threshold = confidence_threshold
        self._ttl_days = ttl_days
        # D5 (Task 15): failure counter bumped whenever the scorer defers.
        # Optional so unit tests / callers without availability wiring work.
        self._availability_counter = availability_counter
        # In-process cached active watchlist; refreshed by the loop every N s.
        self._watchlist: list[str] = []

    # ------------------------------------------------------------------
    def _new_event_id(self) -> str:
        return uuid.uuid4().hex

    def _canonical_raw_path(self, event_id: str, src_staging_path: str,
                            *, consume: bool = True) -> str:
        """Canonicalize the staging raw file to ``events/<event_id>.json``.

        ``consume=False`` COPIES instead of moving, leaving the staging file
        in place.  The deferred path uses this: deferred events deliberately
        skip dedupe recording so a redelivery is RE-SCORED — that redelivered
        envelope still points at the staging path, which must therefore still
        exist or the re-scored event ends up with raw_path="" (no raw text
        for downstream compose).
        """
        canonical_dir = Path(self._data_dir) / "events"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        dst = canonical_dir / f"{event_id}.json"
        try:
            if consume:
                shutil.move(src_staging_path, dst)
            else:
                shutil.copy2(src_staging_path, dst)
        except FileNotFoundError:
            # Staging file gone (test envelopes may not write one); leave path absent.
            return ""
        return str(dst)

    def set_active_watchlist(self, tickers: Sequence[str]) -> None:
        self._watchlist = list(tickers)

    # ------------------------------------------------------------------
    async def process_one(self, env: Envelope) -> TriageResult:
        """Run the full pipeline on one envelope. Always writes a row."""
        # Stage 1: hash / external_id dedupe.
        hit1 = await self._ds1.check(env)
        if hit1:
            ev_id = self._new_event_id()
            insert_event(
                self._conn, event_id=ev_id, source=env.source,
                ingested_ts=env.ingested_ts, salience=None,
                raw_path=self._canonical_raw_path(ev_id, env.raw_path),
                status="duplicate", deduped_of=hit1,
            )
            return TriageResult(event_id=ev_id, status="duplicate",
                                deduped_of=hit1)

        # Stage 2: embedding cosine.  Dispatched to the single-thread executor
        # so that embedder.embed() (CPU-bound encode) runs off the event loop.
        loop = asyncio.get_running_loop()
        hit2 = await loop.run_in_executor(
            self._ds2_executor, self._ds2.check, env.text
        )
        if hit2:
            ev_id = self._new_event_id()
            insert_event(
                self._conn, event_id=ev_id, source=env.source,
                ingested_ts=env.ingested_ts, salience=None,
                raw_path=self._canonical_raw_path(ev_id, env.raw_path),
                status="duplicate", deduped_of=hit2,
            )
            return TriageResult(event_id=ev_id, status="duplicate",
                                deduped_of=hit2)

        # Score salience.
        score: SalienceResult = await self._scorer.score(
            env=env, watchlist=self._watchlist, macro_context="",
        )

        # D5 (Task 15): the scorer could not produce a score (LLM endpoint or
        # parse failure — score.reason carries which).  Degrade LOUDLY, not
        # silently:
        #   - count the failure (in-memory consecutive run + persistent total);
        #   - persist the event so the backlog is observable, but with
        #     salience=NULL (un-scored — `salience >= ?` in the promoter
        #     candidate query excludes NULL, so it can never fire an alert)
        #     and salience_source='deferred' so it is identifiable/retryable;
        #   - deliberately SKIP stage-1/stage-2 dedupe RECORDING and ticker
        #     promotion: a redelivery of the same payload must be RE-SCORED,
        #     not swallowed as a duplicate of the unscored row.
        #
        # Operator note: this return is a HANDLED outcome — _process_entry
        # XACKs the stream entry like any other, so re-scoring relies on the
        # SOURCE re-publishing the payload (poll-based adapters do on their
        # next sweep).  The deferred backlog is discoverable via
        # `SELECT ... WHERE salience_source='deferred'`.
        if score.source == "deferred":
            if self._availability_counter is not None:
                self._availability_counter.record_failure(reason=score.reason)
            ev_id = self._new_event_id()
            insert_event(
                self._conn, event_id=ev_id, source=env.source,
                ingested_ts=env.ingested_ts, salience=None,
                # consume=False: COPY the staging raw file rather than move
                # it — the redelivery that re-scores this payload reads the
                # same staging path and must still find its raw text.
                raw_path=self._canonical_raw_path(ev_id, env.raw_path,
                                                  consume=False),
                status="triaged", deduped_of=None,
                salience_source="deferred",
            )
            log.warning(
                "salience deferred (%s): event %s recorded un-scored; dedupe "
                "recording skipped so a redelivery re-scores", score.reason,
                ev_id,
            )
            return TriageResult(event_id=ev_id, status="triaged",
                                salience=None)
        # Only a REAL LLM round-trip proves the endpoint is healthy.  Cache
        # hits contact nothing, so they must leave the consecutive-failure
        # run untouched — otherwise frequent cache hits during an outage
        # would delay fallback engagement indefinitely.
        if score.source == "llm" and self._availability_counter is not None:
            self._availability_counter.record_success()

        # Resolve tickers: union(source_tags.tickers, mentioned_tickers) → validate.
        candidate = list(env.source_tags.get("tickers", [])) + \
                    [m.ticker for m in score.mentioned_tickers]
        validated = self._validator.filter(candidate)

        # Write event.
        ev_id = self._new_event_id()
        insert_event(
            self._conn, event_id=ev_id, source=env.source,
            ingested_ts=env.ingested_ts, salience=score.salience,
            raw_path=self._canonical_raw_path(ev_id, env.raw_path),
            status="triaged", deduped_of=None,
            salience_source=score.source,   # 'llm' | 'cache'
        )
        # Record fingerprints + embedding (only on non-duplicates).
        await self._ds1.record(env, event_id=ev_id)
        # ds2.record embeds the text (CPU-bound) — also off-thread via the
        # same single-thread executor so sqlite access stays serialized.
        await loop.run_in_executor(
            self._ds2_executor,
            lambda: self._ds2.record(text=env.text, event_id=ev_id),
        )

        # Per-ticker rows + watchlist gate.
        conf_by_ticker = {m.ticker: m.confidence for m in score.mentioned_tickers}
        for t in validated:
            conf = conf_by_ticker.get(t, 0.5)  # source-tag tickers default to 0.5
            insert_event_ticker(self._conn, event_id=ev_id, ticker=t,
                                 confidence=conf)
            auto_promote(
                self._conn, ticker=t, event_id=ev_id,
                salience=score.salience, confidence=conf,
                salience_threshold=self._salience_threshold,
                confidence_threshold=self._confidence_threshold,
                ttl_days=self._ttl_days,
            )

        return TriageResult(event_id=ev_id, status="triaged",
                            salience=score.salience,
                            matched_tickers=score.matched_tickers)


# ----------------------------------------------------------------------
# Consume loop + dead-letter sweep + systemd entry point
# ----------------------------------------------------------------------

async def dead_letter_sweep(
    r: aioredis.Redis,
    *,
    src_stream: str,
    group: str,
    dead_stream: str,
    max_deliveries: int,
) -> int:
    """Move PEL entries with delivery_count >= max_deliveries to ``dead_stream``.

    Returns # of messages moved. Safe to call repeatedly.
    """
    pending = await r.xpending_range(src_stream, group,
                                      min="-", max="+", count=200)
    moved = 0
    for p in pending:
        # max_deliveries is the threshold "this many failed attempts means dead";
        # so times_delivered < max_deliveries → keep trying, otherwise → move.
        if int(p["times_delivered"]) < max_deliveries:
            continue
        msg_id = p["message_id"]
        # Read the message to copy it.
        items = await r.xrange(src_stream, min=msg_id, max=msg_id)
        if not items:
            await r.xack(src_stream, group, msg_id)
            continue
        _, fields = items[0]
        await r.xadd(dead_stream, fields)
        await r.xack(src_stream, group, msg_id)
        moved += 1
    return moved


def _decode_fields(raw_fields):
    """Normalize bytes-or-str fields to a flat str dict."""
    out = {}
    for k, v in raw_fields.items():
        if isinstance(k, bytes):
            k = k.decode("utf-8")
        if isinstance(v, bytes):
            v = v.decode("utf-8")
        out[k] = v
    return out


# Attach to Triage as methods.
async def _process_entry(self, *, env_id, raw_fields, group: str,
                         stream: str) -> bool:
    """Decode + run one PEL/stream entry through the pipeline.

    Returns True if the entry was handled (and XACKed). On failure the
    message is left on the PEL so its delivery count keeps climbing toward
    max_deliveries (where dead_letter_sweep / reclaim dead-letters it).
    """
    try:
        fields = _decode_fields(raw_fields)
        env = Envelope.from_redis_fields(fields)
        await self.process_one(env)
        await self._redis.xack(stream, group, env_id)
        return True
    except Exception:
        log.exception("triage failed for %s; leaving on PEL", env_id)
        return False


async def _reclaim_pending(self, *, group: str, consumer: str, stream: str,
                           batch: int, min_idle_ms: int,
                           dead_stream: Optional[str],
                           max_deliveries: int) -> int:
    """Re-read THIS consumer's stuck pending entries so they actually retry.

    XREADGROUP with `>` only ever delivers brand-new messages, so a message
    that throws in process_one sits on the PEL with times_delivered=1 and is
    never re-read — dead_letter_sweep (which only moves entries with
    times_delivered >= max_deliveries) can therefore never fire. XAUTOCLAIM
    re-claims our own idle pending entries (incrementing delivery count) and
    hands them back for reprocessing. Entries already at/over max_deliveries
    are dead-lettered immediately rather than re-run.

    Fully defensive: any error here is logged and swallowed so a reclaim
    failure can never crash the consume loop.
    """
    handled = 0
    try:
        # XAUTOCLAIM returns (next_cursor, claimed_entries, deleted_ids).
        # Older redis-py returns just (next_cursor, claimed_entries).
        res = await self._redis.xautoclaim(
            name=stream, groupname=group, consumername=consumer,
            min_idle_time=min_idle_ms, start_id="0-0", count=batch,
        )
    except Exception:
        log.exception("xautoclaim failed (reclaim skipped this tick)")
        return 0

    try:
        claimed = res[1] if isinstance(res, (list, tuple)) and len(res) >= 2 else []
    except Exception:
        log.exception("could not parse xautoclaim result"); return 0
    if not claimed:
        return 0

    for env_id, raw_fields in claimed:
        try:
            # Has this entry already been delivered too many times? If so,
            # dead-letter it now instead of re-running a poison message.
            over_limit = False
            try:
                info = await self._redis.xpending_range(
                    stream, group, min=env_id, max=env_id, count=1,
                )
                if info and int(info[0]["times_delivered"]) >= max_deliveries:
                    over_limit = True
            except Exception:
                log.exception("xpending_range failed for %s", env_id)

            if over_limit and dead_stream:
                if raw_fields:
                    await self._redis.xadd(dead_stream, _decode_fields(raw_fields))
                await self._redis.xack(stream, group, env_id)
                log.warning("dead-lettered %s after >= %d deliveries",
                            env_id, max_deliveries)
                continue

            if await self._process_entry(env_id=env_id, raw_fields=raw_fields,
                                         group=group, stream=stream):
                handled += 1
        except Exception:
            log.exception("reclaim of %s crashed; leaving on PEL", env_id)
    return handled


async def _consume_once(self, *, group: str, consumer: str, stream: str,
                         block_ms: int, batch: int,
                         min_idle_ms: int = 60000,
                         dead_stream: Optional[str] = None,
                         max_deliveries: int = 5) -> int:
    """Read one XREADGROUP batch and process each envelope.

    First reclaims this consumer's own stuck pending entries (so failed
    messages actually retry and eventually dead-letter), then reads new
    messages. Successful envelopes are XACKed. Failures stay on the PEL.
    """
    # Reclaim our own idle pending entries first so delivery counts climb.
    handled = await self._reclaim_pending(
        group=group, consumer=consumer, stream=stream, batch=batch,
        min_idle_ms=min_idle_ms, dead_stream=dead_stream,
        max_deliveries=max_deliveries,
    )

    try:
        result = await self._redis.xreadgroup(
            groupname=group, consumername=consumer,
            streams={stream: ">"}, count=batch, block=block_ms,
        )
    except Exception:
        log.exception("XREADGROUP failed"); return handled
    if not result:
        return handled
    for _stream_name, entries in result:
        for env_id, raw_fields in entries:
            if await self._process_entry(env_id=env_id, raw_fields=raw_fields,
                                         group=group, stream=stream):
                handled += 1
    return handled


async def _consume_forever(self, *, group: str, consumer: str, stream: str,
                            block_ms: int, batch: int,
                            min_idle_ms: int = 60000,
                            dead_stream: Optional[str] = None,
                            max_deliveries: int = 5) -> None:
    while True:
        try:
            await self.consume_once(group=group, consumer=consumer,
                                     stream=stream, block_ms=block_ms,
                                     batch=batch, min_idle_ms=min_idle_ms,
                                     dead_stream=dead_stream,
                                     max_deliveries=max_deliveries)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("consume_forever iteration crashed")
            await asyncio.sleep(1)


Triage._process_entry = _process_entry          # type: ignore[attr-defined]
Triage._reclaim_pending = _reclaim_pending      # type: ignore[attr-defined]
Triage.consume_once = _consume_once             # type: ignore[attr-defined]
Triage.consume_forever = _consume_forever       # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Systemd entry point
# ----------------------------------------------------------------------

def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from tradingagents.default_config import DEFAULT_CONFIG as C
    from tradingagents.persistence.db import connect
    from tradingagents.persistence.store import get_active_watchlist
    from tradingagents.sensing.embeddings import SentenceTransformerEmbedder
    from tradingagents.sensing.redis_client import make_redis, ensure_consumer_group

    redis = make_redis(C["sensing_redis_url"])
    conn = connect(C["iic_db_path"])

    # Build the LLM caller from the existing factory, applying the D5
    # availability policy (Task 15): when the role resolves to
    # provider='local', an eager /health + 1-token completion probe runs HERE
    # so a dead endpoint fails FAST and LOUD at startup (mirroring the eager
    # embedder load below).  fallback="none" → refuse to start;
    # fallback="api" → re-resolve to the global API provider (logged,
    # budget-bounded per call).
    from tradingagents.llm_clients.availability import (
        TRIAGE_FAILURE_COUNTER, TRIAGE_FALLBACK_BUDGET,
        AvailabilityCounter, DailyFallbackBudget, LocalEndpointUnavailable,
        resolve_role_llm_global, resolve_role_llm_with_fallback,
    )
    from tradingagents.sensing.salience import maybe_bind_salience_schema
    quick_client, used_fallback = resolve_role_llm_with_fallback(
        "triage_salience", C)
    llm = quick_client.get_llm()
    # Capability-gated: bind json_schema response_format only when the resolved
    # model supports grammar-constrained decoding (local GGUF / llama.cpp).
    # DeepSeek/MiniMax API models have supports_json_schema=False and are left
    # unbound so they never receive an unsupported parameter.
    llm = maybe_bind_salience_schema(llm, quick_client.model)

    role_cfg = C.get("llm_roles", {}).get("triage_salience", {}) or {}
    fallback_mode = (role_cfg.get("fallback") or "none").lower()
    fallback_threshold = int(role_cfg.get("fallback_threshold", 3))
    primary_is_local = (
        (role_cfg.get("provider") or C.get("llm_provider") or "").lower()
        == "local"
    )
    # Failure counter: bumped by process_one on every deferred score; read
    # here to drive runtime fallback engagement, persisted via ops_counters
    # for the soak gate (Task 16) and the self-alert seam (Task 17).
    #
    # DEDICATED cross-thread connection: call_llm runs inside
    # asyncio.to_thread (SalienceScorer dispatches the sync LLM call to a
    # worker thread), so fallback_budget.try_consume persists ops_counters
    # OFF the main thread.  The main `conn` is main-thread-bound
    # (check_same_thread=True) — using it there raises
    # sqlite3.ProgrammingError, which the budget's except sqlite3.Error
    # swallows, silently degrading persistence to in-memory.
    #
    # ONE conn, ONE lock: the counter (record_failure, event-loop thread) and
    # the budget (try_consume, to_thread workers) share this conn, so they
    # must also share the lock that serializes every access to it.  Each
    # object's default per-instance lock would leave cross-object conn calls
    # unserialized — under interleaving the C-level sqlite3 module raises
    # SystemError('error return without exception set') (NOT a sqlite3.Error,
    # so it escapes the persistence except-nets into process_one) and loses
    # persisted bumps.
    # Task 17 self-alert: when the consecutive deferred run reaches the
    # role's fallback_threshold (the documented alert-threshold source — no
    # new config key), the counter fires the operator self-alert EXACTLY
    # ONCE per outage (debounced in the counter, re-armed by
    # record_success).  Lock discipline: record_failure releases the SHARED
    # avail_lock BEFORE invoking the callback, so the (possibly blocking)
    # transport send never holds the lock the to_thread budget workers need.
    # The callback runs on whichever thread recorded the crossing failure —
    # here that is process_one on the event-loop thread; at most one
    # blocking send per outage is an accepted stall.
    from tradingagents.ops import self_alert
    alerter = self_alert.build_self_alerter(C)
    avail_conn = _open_cross_thread_conn(C["iic_db_path"])
    avail_lock = threading.Lock()
    availability_counter = AvailabilityCounter(
        name=TRIAGE_FAILURE_COUNTER, conn=avail_conn, lock=avail_lock,
        alert_threshold=fallback_threshold,
        on_threshold=alerter.endpoint_down_callback)
    fallback_budget = DailyFallbackBudget(
        name=TRIAGE_FALLBACK_BUDGET,
        max_per_day=int(role_cfg.get("fallback_daily_budget", 500)),
        conn=avail_conn, lock=avail_lock,
    )
    # Mutable holder so runtime fallback engagement can swap the model for
    # subsequent calls without rebuilding the closure.
    _llm_state = {"llm": llm, "used_fallback": used_fallback}

    def call_llm(prompt: str) -> str:
        # Runtime fallback (D5): after fallback_threshold CONSECUTIVE failures
        # (counted by process_one when the scorer defers), re-resolve this
        # role to the global API provider.  Engagement is sticky for the
        # process lifetime; every fallback call burns the hard daily budget,
        # and when it is exhausted the raise below makes the scorer defer —
        # degradation stays loud and counted, never silent.
        if (fallback_mode == "api" and primary_is_local
                and not _llm_state["used_fallback"]
                and availability_counter.consecutive >= fallback_threshold):
            fb = resolve_role_llm_global("triage_salience", C)
            _llm_state["llm"] = maybe_bind_salience_schema(
                fb.get_llm(), fb.model)
            _llm_state["used_fallback"] = True
        if _llm_state["used_fallback"] and not fallback_budget.try_consume():
            raise LocalEndpointUnavailable(
                f"fallback daily budget exhausted for role triage_salience "
                f"(max={fallback_budget.max_per_day}/day)"
            )
        # LangChain chat models expose .invoke for str-or-message input.
        out = _llm_state["llm"].invoke(prompt)
        return getattr(out, "content", str(out))

    # Eagerly load the embedder model so a missing dep / failed download
    # fails FAST and LOUD at startup, before the soak clock starts —
    # instead of every event silently zeroing out inside the consume loop
    # (NRestarts=0, events=0 → false-FAIL gate). MockEmbedder has no load(),
    # but _main always builds the real SentenceTransformerEmbedder.
    embedder = SentenceTransformerEmbedder(C["sensing_embedder_model"])
    _load = getattr(embedder, "load", None)
    if callable(_load):
        _load()

    t = Triage(
        conn=conn, redis=redis,
        embedder=embedder,
        llm_call=call_llm,
        data_dir=C["iic_data_dir"],
        cosine_threshold=C["sensing_dedupe_cosine_threshold"],
        window_hours=C["sensing_dedupe_window_hours"],
        fingerprint_ttl_hours=C["sensing_fingerprint_ttl_hours"],
        salience_threshold=C["sensing_watchlist_salience_threshold"],
        confidence_threshold=C["sensing_watchlist_confidence_threshold"],
        salience_cache_ttl_seconds=C["sensing_salience_cache_ttl_seconds"],
        ttl_days=C["sensing_watchlist_ttl_days"],
        availability_counter=availability_counter,
    )

    async def run() -> None:
        await ensure_consumer_group(
            redis, stream=C["sensing_ingest_stream"], group=C["sensing_consumer_group"],
        )
        # Watchlist refresher: every N seconds, refresh in-process cache.
        async def refresher():
            while True:
                try:
                    t.set_active_watchlist(get_active_watchlist(conn))
                except Exception:
                    log.exception("watchlist refresh failed")
                await asyncio.sleep(C["sensing_watchlist_refresh_seconds"])

        # Dead-letter sweep every minute.
        async def reaper():
            while True:
                try:
                    await dead_letter_sweep(
                        redis,
                        src_stream=C["sensing_ingest_stream"],
                        group=C["sensing_consumer_group"],
                        dead_stream=C["sensing_dead_stream"],
                        max_deliveries=C["sensing_triage_max_failures"],
                    )
                except Exception:
                    log.exception("dead-letter sweep failed")
                await asyncio.sleep(60)

        # N consumers + refresher + reaper.
        tasks = [refresher(), reaper()]
        for i in range(C["sensing_triage_consumers"]):
            tasks.append(t.consume_forever(
                group=C["sensing_consumer_group"],
                consumer=f"c{i}",
                stream=C["sensing_ingest_stream"],
                block_ms=5000, batch=10,
                # Reclaim our own stuck PEL entries so failures actually retry
                # and eventually dead-letter instead of leaking forever.
                min_idle_ms=60000,
                dead_stream=C["sensing_dead_stream"],
                max_deliveries=C["sensing_triage_max_failures"],
            ))
        await asyncio.gather(*tasks)

    asyncio.run(run())


if __name__ == "__main__":
    _main()
