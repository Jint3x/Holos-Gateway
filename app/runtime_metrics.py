"""Small, application-scoped measurements of worker resources and saturation."""

import logging
from contextlib import asynccontextmanager
from time import monotonic

import anyio
import psutil
from anyio.to_thread import current_default_thread_limiter
from opentelemetry.metrics import Observation

logger = logging.getLogger("holos.runtime")


class RuntimeMetrics:
    def __init__(self, meter):
        self.process = psutil.Process()
        self.started = monotonic()
        self.pool = None
        self.failed_observations = set()
        meter.create_observable_counter(
            "process.cpu.time",
            callbacks=[self._observe_cpu],
            unit="s",
            description="Worker CPU seconds; rate gives CPU cores consumed.",
        )
        meter.create_observable_gauge(
            "process.memory.usage",
            callbacks=[self._observe_memory],
            unit="By",
            description="Resident physical memory used by this worker (RSS).",
        )
        meter.create_observable_gauge(
            "process.open_file_descriptor.count",
            callbacks=[self._observe_file_descriptors],
            unit="{file_descriptor}",
            description="Open file descriptors, when supported by the OS.",
        )
        meter.create_observable_gauge(
            "holos.runtime.uptime",
            callbacks=[lambda _: [Observation(max(0, monotonic() - self.started))]],
            unit="s",
            description="Seconds since this worker's telemetry was initialized.",
        )
        meter.create_observable_gauge(
            "holos.telemetry.heartbeat",
            callbacks=[lambda _: [Observation(1)]],
            description="Present even without requests; absence detects lost telemetry.",
        )
        for index, name in enumerate(("busy", "capacity", "waiting")):
            meter.create_observable_gauge(
                f"holos.runtime.thread_pool.{name}",
                callbacks=[self._pool_callback(index)],
                description=f"AnyIO default thread limiter {name} tokens/tasks.",
            )
        self.loop_delay = meter.create_histogram(
            "holos.runtime.event_loop.delay",
            unit="s",
            description="Delay in running a one-second scheduler probe.",
            explicit_bucket_boundaries_advisory=(
                0.001,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1,
                2.5,
                5,
            ),
        )

    def _read(self, name, read):
        try:
            result = read()
        except psutil.Error, OSError, NotImplementedError, AttributeError:
            if name not in self.failed_observations:
                # Log only the reviewed category, never OS exception text.
                logger.warning("runtime.observation.unavailable: %s", name)
                self.failed_observations.add(name)
            return []
        self.failed_observations.discard(name)
        return result

    def _observe_cpu(self, _):
        def read():
            times = self.process.cpu_times()
            return [
                Observation(times.user, {"cpu.mode": "user"}),
                Observation(times.system, {"cpu.mode": "system"}),
            ]

        return self._read("cpu", read)

    def _observe_memory(self, _):
        return self._read(
            "memory", lambda: [Observation(self.process.memory_info().rss)]
        )

    def _observe_file_descriptors(self, _):
        # This measurement is intentionally absent on platforms without num_fds.
        if not hasattr(self.process, "num_fds"):
            return []
        return self._read(
            "file_descriptors", lambda: [Observation(self.process.num_fds())]
        )

    def _pool_callback(self, index):
        def observe(_):
            return [] if self.pool is None else [Observation(self.pool[index])]

        return observe

    def sample_scheduler(self, expected, observed, statistics):
        """Record one probe; separate from scheduling for deterministic testing."""
        self.loop_delay.record(max(0, observed - expected))
        self.pool = (
            statistics.borrowed_tokens,
            statistics.total_tokens,
            statistics.tasks_waiting,
        )

    async def _monitor(self, limiter):
        while True:
            expected = monotonic() + 1
            await anyio.sleep(1)
            self.sample_scheduler(expected, monotonic(), limiter.statistics())

    @asynccontextmanager
    async def monitor(self):
        limiter = current_default_thread_limiter()
        stats = limiter.statistics()
        self.pool = (stats.borrowed_tokens, stats.total_tokens, stats.tasks_waiting)
        async with anyio.create_task_group() as group:
            group.start_soon(self._monitor, limiter)
            try:
                yield
            finally:
                group.cancel_scope.cancel()
