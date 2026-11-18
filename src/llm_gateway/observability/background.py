"""Asynchronous non-blocking in-process queue for batched usage log persistence."""

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.db.models import UsageLog
from llm_gateway.db.session import get_db_context

logger = logging.getLogger("llm_gateway.usage_queue")


@dataclass
class UsageLogEntry:
    """Represents a single request telemetry record to be saved to Postgres."""

    request_id: str
    api_key_id: str | None
    requested_model: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: float
    cache_hit: bool
    fallback_used: bool
    attempts: int
    status_code: int
    alias: str | None = None
    error_type: str | None = None
    streaming: bool = False
    estimated_tokens: bool = False
    created_at: datetime | None = None
    prompt_text: str | None = None
    response_text: str | None = None


class UsageLogQueue:
    """Asynchronous worker that consumes usage log records and writes them in batches."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._queue: asyncio.Queue[UsageLogEntry] = asyncio.Queue(
            maxsize=self.settings.USAGE_LOG_QUEUE_MAX_SIZE
        )
        self._worker_task: asyncio.Task | None = None
        self._running = False
        self.dropped_logs_count = 0

    def start(self) -> None:
        """Start the background consumer task."""
        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._worker_task = asyncio.create_task(self._process_queue())
            logger.info("UsageLog background queue consumer started.")

    async def stop(self) -> None:
        """Gracefully drain and flush all remaining queued records before exiting."""
        logger.info("Stopping UsageLog queue consumer and flushing pending records...")
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        # Drain any residual items left in the queue
        await self.flush()

    def enqueue(self, entry: UsageLogEntry) -> bool:
        """Non-blocking enqueue. If queue is full, drop entry and increment metric."""
        if entry.created_at is None:
            entry.created_at = datetime.now(UTC)

        try:
            self._queue.put_nowait(entry)
            return True
        except asyncio.QueueFull:
            self.dropped_logs_count += 1
            logger.error(
                f"UsageLog queue is full ({self.settings.USAGE_LOG_QUEUE_MAX_SIZE}); "
                f"dropping log for request {entry.request_id}."
            )
            return False

    async def flush(self) -> None:
        """Drain all current items in queue and write immediately to database."""
        batch: list[UsageLogEntry] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._write_batch(batch)

    async def _write_batch(self, batch: list[UsageLogEntry]) -> None:
        """Persist a batch of UsageLogEntry records to the database."""
        records: list[UsageLog] = []
        log_payloads = self.settings.LOG_PAYLOADS

        for entry in batch:
            # Privacy: calculate SHA-256 hashes of payloads
            prompt_hash = (
                hashlib.sha256(entry.prompt_text.encode("utf-8")).hexdigest()
                if entry.prompt_text
                else None
            )
            response_hash = (
                hashlib.sha256(entry.response_text.encode("utf-8")).hexdigest()
                if entry.response_text
                else None
            )

            records.append(
                UsageLog(
                    request_id=entry.request_id,
                    api_key_id=entry.api_key_id,
                    created_at=entry.created_at or datetime.now(UTC),
                    alias=entry.alias,
                    requested_model=entry.requested_model,
                    provider=entry.provider,
                    model=entry.model,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cost_usd=entry.cost_usd,
                    latency_ms=entry.latency_ms,
                    cache_hit=entry.cache_hit,
                    fallback_used=entry.fallback_used,
                    attempts=entry.attempts,
                    status_code=entry.status_code,
                    error_type=entry.error_type,
                    streaming=entry.streaming,
                    estimated_tokens=entry.estimated_tokens,
                    prompt_hash=prompt_hash,
                    response_hash=response_hash,
                    prompt_payload=entry.prompt_text if log_payloads else None,
                    response_payload=entry.response_text if log_payloads else None,
                )
            )

        try:
            async with get_db_context() as session:
                session.add_all(records)
                await session.commit()
            logger.debug(f"Persisted batch of {len(records)} usage logs to database.")
        except Exception as exc:
            logger.error(f"Failed to write usage logs batch to database: {exc}")

    async def _process_queue(self) -> None:
        """Background consumer loop that batches records by count or flush interval."""
        batch_size = self.settings.USAGE_LOG_BATCH_SIZE
        interval = self.settings.USAGE_LOG_FLUSH_INTERVAL_SECONDS

        while self._running:
            batch: list[UsageLogEntry] = []
            try:
                # Wait for at least one item
                entry = await asyncio.wait_for(self._queue.get(), timeout=interval)
                batch.append(entry)
                self._queue.task_done()

                # Collect any remaining items up to batch_size without waiting
                while len(batch) < batch_size and not self._queue.empty():
                    try:
                        batch.append(self._queue.get_nowait())
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    await self._write_batch(batch)

            except TimeoutError:
                # No new items arrived within interval
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Unexpected error in usage log worker: {exc}")
                await asyncio.sleep(0.5)


_usage_queue: UsageLogQueue | None = None


def get_usage_queue() -> UsageLogQueue:
    """Singleton accessor for the global usage log queue."""
    global _usage_queue
    if _usage_queue is None:
        _usage_queue = UsageLogQueue()
    return _usage_queue
