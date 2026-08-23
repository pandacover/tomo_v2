from __future__ import annotations

import gc
import io
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst, MessageAttachment, RuntimeConfig
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta
from tomo_core.sandbox_inbound import run_once
from tomo_core.sandbox_protocol import encode_inbound, iter_event_markers
from tomo_core.vision import DownloadedAttachment, ProviderVisionInterpreter


RESULT_MARKER = "TOMO_BENCH_RESULT="
EVENT_MARKER = "TOMO_BENCH_EVENT="
PLAN = (
    '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],'
    '"move_sequence":["answer"],"response_goal":"answer","confidence":"high"}\n'
)


def emit(event: str, **values: object) -> None:
    print(EVENT_MARKER + json.dumps({"event": event, **values}, separators=(",", ":")), flush=True)


def read_number(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def cgroup_memory() -> dict[str, int | None]:
    return {
        "currentBytes": read_number("/sys/fs/cgroup/memory.current"),
        "peakBytes": read_number("/sys/fs/cgroup/memory.peak"),
        "limitBytes": read_number("/sys/fs/cgroup/memory.max"),
    }


def process_memory() -> dict[str, int | None]:
    high_water_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    current_kib = None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                current_kib = int(line.split()[1])
                break
    except OSError:
        pass
    return {"currentKiB": current_kib, "highWaterKiB": high_water_kib}


def directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if any(part in {".git", ".wrangler", "node_modules", "results"} for part in path.parts):
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


class FakeProvider:
    name = "cloudflare_benchmark_fake"
    supports_images_in = True
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.vision_calls = 0

    def stream(self, messages: list[dict[str, object]], *, tools=(), actor_id=None) -> Iterator[object]:
        del messages, tools, actor_id
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        payload = PLAN + '{"type":"frame","text":"Cloudflare benchmark response."}\n'
        midpoint = len(payload) // 2
        for chunk in (payload[:midpoint], payload[midpoint:]):
            yield ProviderTextDelta(chunk)
            time.sleep(0.05)
        yield ProviderStreamCompleted("stop", input_tokens=20, output_tokens=8)

    def stream_structured(self, messages: list[dict[str, object]], *, response_format, actor_id=None) -> Iterator[object]:
        del messages, response_format, actor_id
        self.vision_calls += 1
        observation = json.dumps(
            {
                "summary": "synthetic benchmark image",
                "visible_text": [],
                "relevant_details": ["5000 by 4000 pixels"],
                "uncertainties": [],
            },
            separators=(",", ":"),
        )
        yield ProviderTextDelta(observation)
        yield ProviderStreamCompleted("stop", input_tokens=10, output_tokens=6)

    def complete(self, messages: list[dict[str, str]], actor_id=None) -> str:
        del messages, actor_id
        return "Cloudflare benchmark response."


class StaticAttachmentReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, attachment: MessageAttachment) -> DownloadedAttachment:
        del attachment
        return DownloadedAttachment(self.payload, "image/jpeg")


class RecordingOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_times_ms: list[int] = []
        self.started_ns = time.monotonic_ns()

    def flush(self) -> None:
        self.flush_times_ms.append((time.monotonic_ns() - self.started_ns) // 1_000_000)
        value = self.getvalue()
        last_line = value.rstrip("\n").split("\n")[-1] if value else ""
        if last_line:
            emit("protocol_flush", atMs=self.flush_times_ms[-1], bytes=len(last_line.encode("utf-8")))


def synthetic_image(*, maximum: bool) -> bytes:
    width, height = (5000, 4000) if maximum else (2560, 1920)
    fixture = Path("/opt/tomo/benchmark-image-max.jpg" if maximum else "/opt/tomo/benchmark-image.jpg")
    if fixture.is_file():
        return fixture.read_bytes()
    with tempfile.TemporaryDirectory(prefix="tomo-image-fixture-") as directory:
        fixture = Path(directory) / "benchmark-image.jpg"
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from PIL import Image; import sys; "
                    f"image=Image.new('RGB',({width},{height}),(72,110,160)); "
                    "image.save(sys.argv[1],format='JPEG',quality=90); image.close()"
                ),
                str(fixture),
            ],
            check=True,
        )
        return fixture.read_bytes()


def verify_sqlite(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        ftsRows = connection.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'cloudflare'"
        ).fetchone()[0]
        messageRows = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
    return {"integrity": integrity, "ftsRows": ftsRows, "messageRows": messageRows}


def run() -> dict[str, object]:
    scenario = os.getenv("TOMO_BENCH_SCENARIO", "text")
    owner_id = os.getenv("TOMO_BENCH_OWNER_ID", "owner-1")
    delay_seconds = float(os.getenv("TOMO_BENCH_DELAY_SECONDS", "0.05"))
    if scenario not in {"text", "image", "image-max", "wait"}:
        raise ValueError("invalid benchmark scenario")
    if not 0 <= delay_seconds <= 720:
        raise ValueError("invalid benchmark delay")

    started_ns = time.monotonic_ns()
    cpu_started = time.process_time_ns()
    emit("runtime_imported", scenario=scenario, ownerId=owner_id)
    provider = FakeProvider(delay_seconds if scenario in {"text", "wait"} else 0.05)
    image_payload = synthetic_image(maximum=scenario == "image-max") if scenario.startswith("image") else None
    vision = (
        ProviderVisionInterpreter(provider, StaticAttachmentReader(image_payload))
        if image_payload is not None
        else None
    )
    attachments = (
        tuple(
            MessageAttachment("image", file_id=f"synthetic-{index}", mime_type="image/jpeg")
            for index in range(8)
        )
        if scenario.startswith("image")
        else ()
    )

    with tempfile.TemporaryDirectory(prefix="tomo-cloudflare-bench-") as temp_dir:
        data_dir = Path(temp_dir)
        burst = InputBurst(
            "benchmark-burst",
            "benchmark-generation",
            1,
            (
                InboundMessage(
                    1,
                    1,
                    InboundEnvelope(
                        "telegram",
                        owner_id,
                        "benchmark-message",
                        "Cloudflare benchmark input",
                        attachments=attachments,
                    ),
                ),
            ),
        )
        stdout = RecordingOutput()
        soul_path = Path("/opt/tomo/SOUL.md")
        if not soul_path.is_file():
            soul_path = Path(__file__).resolve().parent.parent / "SOUL.md"
        config = RuntimeConfig(
            data_dir=str(data_dir),
            soul_path=str(soul_path),
            owner_id=owner_id,
            local_work_dir=str(data_dir / "work"),
            # The fake provider deliberately sleeps; keep the turn ceiling above it.
            max_turn_seconds=delay_seconds + 60,
        )
        exit_code = run_once(
            io.StringIO(encode_inbound("benchmark-request", burst)),
            stdout,
            config=config,
            provider=provider,
            vision_interpreter=vision,
        )
        events = list(
            iter_event_markers(
                stdout.getvalue().splitlines(keepends=True),
                "benchmark-request",
                "benchmark-generation",
            )
        )
        databases = tuple((data_dir / "work").glob("*.sqlite3"))
        if len(databases) != 1:
            raise RuntimeError("benchmark working database was not created")
        database = databases[0]
        sqlite_result = verify_sqlite(database)
        sqlite_result["checkpointFiles"] = len(tuple(data_dir.glob("*.checkpoint.*")))
        data_bytes = directory_bytes(data_dir)

    gc.collect()
    elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) // 1_000_000
    return {
        "passed": exit_code == 0 and sqlite_result["integrity"] == "ok" and bool(events),
        "scenario": scenario,
        "ownerId": owner_id,
        "elapsedMs": elapsed_ms,
        "cpuMs": cpu_ms,
        "processMemory": process_memory(),
        "cgroupMemory": cgroup_memory(),
        "runtimeImageBytes": directory_bytes(
            Path("/opt/tomo")
            if Path("/opt/tomo/SOUL.md").is_file()
            else Path(__file__).resolve().parent.parent
        ),
        "dataBytes": data_bytes,
        "imageInputBytes": len(image_payload) if image_payload is not None else 0,
        "providerCalls": provider.calls,
        "visionCalls": provider.vision_calls,
        "protocolEvents": len(events),
        "protocolFlushMs": stdout.flush_times_ms,
        "sqlite": sqlite_result,
        "python": sys.version.split()[0],
    }


if __name__ == "__main__":
    try:
        result = run()
    except Exception as error:
        result = {
            "passed": False,
            "errorType": type(error).__name__,
            "processMemory": process_memory(),
            "cgroupMemory": cgroup_memory(),
        }
    print(RESULT_MARKER + json.dumps(result, separators=(",", ":")), flush=True)
