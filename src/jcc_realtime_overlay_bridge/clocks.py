"""Local clock binding, not trust in a producer-supplied domain name."""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import platform
import re
import subprocess
import time

from jcc_realtime_probe.events import EVENT_SCHEMA, validate_source_clock
from jcc_realtime_probe.processing import PROCESSING_KINDS

_SEAL = object()
MAX_NS = 2**63 - 1


def nanoseconds(value):
    return type(value) is int and 0 < value <= MAX_NS


def host_signature():
    """Private same-boot check. Do not publish the underlying machine values."""
    info = time.get_clock_info("monotonic")
    if not info.monotonic or info.adjustable:
        raise ValueError("uncertain_host_clock")
    if platform.system() == "Darwin":
        boot = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                              check=True, capture_output=True, timeout=2).stdout.strip()
    elif platform.system() == "Linux":
        boot = Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()
    else:
        raise ValueError("unsupported_host_clock")
    if not boot:
        raise ValueError("uncertain_boot_epoch")
    return dict(boot_digest=hashlib.sha256(boot).hexdigest(), implementation=info.implementation)


@dataclass(frozen=True)
class ClockBinding:
    run_id: str
    source_kind: str
    clock_kind: str
    assurance: str
    clock: object = field(repr=False)
    owner_pid: int = field(repr=False)
    _seal: object = field(repr=False)
    processing_kind: str

    def validate(self):
        validate_source_clock(self.source_kind, self.clock_kind)
        if (self._seal is not _SEAL or self.owner_pid != os.getpid()
                or not re.fullmatch(r"[0-9a-f]{16}", self.run_id)
                or self.source_kind not in {"synthetic", "replay", "live"}
                or self.clock_kind not in {"host_monotonic_ns", "virtual_monotonic_ns"}
                or self.processing_kind not in PROCESSING_KINDS or not callable(self.clock)):
            raise ValueError("unbound_clock")


def bind_same_process(sink):
    """Consumer will use exactly the EventSink's callable, including in tests."""
    from jcc_realtime_probe.events import EventSink
    if type(sink) is not EventSink:
        raise ValueError("producer_not_bound")
    if sink.clock_kind == "host_monotonic_ns" and sink.clock_ns is not time.monotonic_ns:
        raise ValueError("host_clock_callable_mismatch")
    assurance = ("same_process_shared_virtual_callable" if sink.clock_kind == "virtual_monotonic_ns"
                 else "same_process_shared_host_monotonic")
    binding = ClockBinding(sink.run_id, sink.source_kind, sink.clock_kind, assurance,
                           sink.clock_ns, os.getpid(), _SEAL, sink.processing_kind)
    binding.validate()
    return binding


def spawned_hello(nonce, run_id):
    return dict(kind="bridge-local-clock-v2", nonce=nonce, run_id=run_id, pid=os.getpid(),
                source_kind="synthetic", clock_kind="host_monotonic_ns",
                processing_kind="simulated_work", event_schema=EVENT_SCHEMA,
                **host_signature(), now_ns=time.monotonic_ns())


def bind_spawned(hello, *, nonce, run_id, child_pid, sent_ns, received_ns):
    """Only called for this tool's freshly spawned child on its private pipe.

    The child is paused until the consumer exists. This is an accidental clock
    mix-up guard, not attestation against malicious code on the same account.
    """
    expected_keys = {"kind", "nonce", "run_id", "pid", "source_kind", "clock_kind",
                     "processing_kind", "event_schema", "boot_digest", "implementation", "now_ns"}
    if type(hello) is not dict or set(hello) != expected_keys:
        raise ValueError("clock_handshake_shape")
    signature = host_signature()
    if (hello["kind"] != "bridge-local-clock-v2" or hello["nonce"] != nonce
            or hello["run_id"] != run_id or type(hello["pid"]) is not int
            or hello["pid"] != child_pid or hello["source_kind"] != "synthetic"
            or hello["clock_kind"] != "host_monotonic_ns"
            or hello["processing_kind"] != "simulated_work" or hello["event_schema"] != EVENT_SCHEMA
            or any(hello[k] != v for k, v in signature.items())
            or not all(nanoseconds(v) for v in (sent_ns, hello["now_ns"], received_ns))
            or not sent_ns <= hello["now_ns"] <= received_ns):
        raise ValueError("clock_handshake_unproven")
    return ClockBinding(run_id, "synthetic", "host_monotonic_ns",
                        "spawned_local_same_boot_monotonic", time.monotonic_ns, os.getpid(), _SEAL,
                        "simulated_work")
