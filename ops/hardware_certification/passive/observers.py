"""Bounded passive observations over receive-only capability transports."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from modules.printers.printer_models import normalize_model_name
from modules.printers.telemetry.state import PrinterStatus

from .parsers import (
    ParsedSample, ParseError, parse_bambu_sample, parse_elegoo_sample,
    parse_moonraker_sample, parse_prusalink_sample, safe_version,
)
from .transports import PassivePolicyError, decode_bounded_json_object


class ObservationError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ObservationSummary:
    samples: tuple[ParsedSample, ...]
    capabilities: tuple[str, ...]
    firmware_version: str = "unknown"
    api_version: str = "unknown"
    freshness_seconds: float = 0.0
    observed_model_family: str | None = None
    reconnect_count: int = 0


MOON_QUERY = "/printer/objects/query?extruder&fan&gcode_move&heater_bed&print_stats&virtual_sdcard&webhooks"
MAX_ELEGOO_FRAMES = 4
UNKNOWN_VERSION_SENTINELS = frozenset({
    "invalid", "missing", "na", "nil", "none", "notapplicable", "notavailable",
    "notprovided", "notreported", "notset", "null", "pending", "tbd", "undefined",
    "unavailable", "unknown", "unset",
})


def _required_device_version(value: object) -> str:
    if not isinstance(value, str):
        raise ObservationError("device_version_mismatch")
    normalized = value.strip()
    placeholder_key = "".join(
        character for character in normalized.casefold() if character.isalnum()
    )
    if not normalized or placeholder_key in UNKNOWN_VERSION_SENTINELS:
        raise ObservationError("device_version_mismatch")
    version = safe_version(normalized)
    if version == "unknown":
        raise ObservationError("device_version_mismatch")
    return version


def _consistent(
    samples: list[ParsedSample], observed_at: list[float],
    model_hints: Iterable[str | None] = (),
    freshness_tokens: Iterable[str] = (),
    counter_advanced: bool = False,
) -> ObservationSummary:
    if len(samples) < 2:
        raise ObservationError("insufficient_samples")
    if len(observed_at) != len(samples):
        raise ObservationError("sample_freshness_invalid")
    freshness = observed_at[-1] - observed_at[0]
    if not 0 < freshness <= 30:
        raise ObservationError("sample_freshness_invalid")
    tokens = list(freshness_tokens)
    if len(tokens) != len(samples) or (tokens[0] == tokens[-1] and not counter_advanced):
        raise ObservationError("sample_freshness_invalid")
    capabilities = set(samples[0].capabilities)
    for sample in samples[1:]:
        capabilities &= set(sample.capabilities)
    if "status" not in capabilities:
        raise ObservationError("capability_shape_invalid")
    models = {
        str(value).strip() for value in [
            *(sample.model_family for sample in samples), *model_hints,
        ] if value and str(value).strip()
    }
    if len({model.casefold() for model in models}) > 1:
        raise ObservationError("model_family_mismatch")
    return ObservationSummary(
        tuple(samples), tuple(sorted(capabilities)), freshness_seconds=freshness,
        observed_model_family=next(iter(models), None),
    )


def _freshness_token(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _known_fields(payload: dict, names: tuple[str, ...]) -> dict:
    return {name: payload[name] for name in names if name in payload}


def _strict_counter(values: list[object]) -> bool:
    if not any(value is not None for value in values):
        return False
    if len(values) != 2 or any(value is None or isinstance(value, bool) for value in values):
        raise ObservationError("sample_freshness_invalid")
    try:
        first, second = (float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ObservationError("sample_freshness_invalid") from exc
    if second <= first:
        raise ObservationError("sample_freshness_invalid")
    return True


def _moonraker_freshness_projection(status: dict) -> dict:
    fields = {
        "print_stats": ("state", "filename", "print_duration", "filament_used"),
        "virtual_sdcard": ("progress",),
        "heater_bed": ("temperature", "target"),
        "extruder": ("temperature", "target"),
        "display_status": ("progress", "message"),
        "idle_timeout": ("state", "printing_time"),
        "gcode_move": ("speed_factor", "extrude_factor"),
        "webhooks": ("state", "state_message"),
        "fan": ("speed",),
    }
    projected = {}
    for name, names in fields.items():
        value = status.get(name)
        if isinstance(value, dict):
            projected[name] = _known_fields(value, names)
    stats = status.get("print_stats")
    info = stats.get("info") if isinstance(stats, dict) else None
    if isinstance(info, dict):
        projected["print_stats_info"] = _known_fields(info, ("current_layer", "total_layer"))
    return projected


def observe_bambu(messages: Iterable[bytes | str | dict], *, clock: Callable[[], float] = time.monotonic) -> ObservationSummary:
    previous = PrinterStatus.initial()
    samples: list[ParsedSample] = []
    observed_at: list[float] = []
    freshness_tokens: list[str] = []
    counters: list[object] = []
    for payload in messages:
        try:
            timestamp = clock()
            bounded = decode_bounded_json_object(payload)
            sample, previous = parse_bambu_sample(bounded, previous=previous, timestamp=timestamp)
        except (ParseError, PassivePolicyError) as exc:
            raise ObservationError("malformed_telemetry") from exc
        samples.append(sample)
        observed_at.append(timestamp)
        print_section = bounded.get("print") if isinstance(bounded.get("print"), dict) else {}
        freshness_tokens.append(_freshness_token(_known_fields(print_section, (
            "gcode_state", "mc_percent", "layer_num", "total_layer_num", "mc_remaining_time",
            "bed_temper", "bed_target_temper", "nozzle_temper", "nozzle_target_temper",
            "gcode_file", "subtask_id", "ams_status", "stg_cur", "print_error",
        ))))
        counters.append(print_section.get("msg", print_section.get("sequence_id")))
        if len(samples) == 2:
            break
    counter_advanced = _strict_counter(counters)
    summary = _consistent(
        samples, observed_at, freshness_tokens=freshness_tokens,
        counter_advanced=counter_advanced,
    )
    capabilities = set(summary.capabilities)
    if any(sample.ams_slots is not None for sample in samples):
        capabilities.add("ams_read")
    return ObservationSummary(
        summary.samples, tuple(sorted(capabilities)),
        freshness_seconds=summary.freshness_seconds,
        observed_model_family=summary.observed_model_family,
    )


def observe_moonraker(transport, *, clock: Callable[[], float] = time.monotonic) -> ObservationSummary:
    try:
        server = transport.get("/server/info")
        objects = transport.get("/printer/objects/list")
        samples = []
        observed_at = []
        freshness_tokens = []
        eventtimes = []
        for _ in range(2):
            payload = transport.get(MOON_QUERY)
            samples.append(parse_moonraker_sample(payload))
            observed_at.append(clock())
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            eventtimes.append(result.get("eventtime"))
            status = result.get("status") if isinstance(result.get("status"), dict) else {}
            freshness_tokens.append(_freshness_token(_moonraker_freshness_projection(status)))
    except (ParseError, ValueError) as exc:
        raise ObservationError("malformed_or_unavailable") from exc
    counter_advanced = _strict_counter(eventtimes)
    server_result = server.get("result") if isinstance(server.get("result"), dict) else {}
    summary = _consistent(
        # Moonraker's documented /server/info contract does not expose printer
        # model identity. Do not accept vendor extensions as certification proof.
        samples, observed_at, [],
        freshness_tokens, counter_advanced,
    )
    object_result = objects.get("result") if isinstance(objects.get("result"), dict) else {}
    available = object_result.get("objects") if isinstance(object_result.get("objects"), list) else []
    capabilities = set(summary.capabilities)
    if "heater_bed" in available:
        capabilities.add("heated_bed")
    return ObservationSummary(
        summary.samples, tuple(sorted(capabilities)),
        api_version=safe_version(server_result.get("moonraker_version")),
        freshness_seconds=summary.freshness_seconds,
        observed_model_family=summary.observed_model_family,
    )


def observe_prusalink(
    transport, headers: dict[str, str] | None = None,
    *, clock: Callable[[], float] = time.monotonic,
) -> ObservationSummary:
    try:
        version = transport.get("/api/version", headers=headers)
        samples = []
        observed_at = []
        freshness_tokens = []
        counters = []
        for _ in range(2):
            status = transport.get("/api/v1/status", headers=headers)
            job = transport.get("/api/job", headers=headers)
            samples.append(parse_prusalink_sample(
                status, job,
            ))
            observed_at.append(clock())
            printer = status.get("printer") if isinstance(status.get("printer"), dict) else {}
            job_root = job.get("job") if isinstance(job.get("job"), dict) else {}
            file_root = job_root.get("file") if isinstance(job_root.get("file"), dict) else {}
            freshness_tokens.append(_freshness_token({
                "printer": _known_fields(printer, (
                    "state", "temp_bed", "target_bed", "temp_nozzle", "target_nozzle",
                    "axis_z", "flow", "speed", "fan_hotend", "fan_print",
                )),
                "job": _known_fields(job_root, ("id", "progress", "time_printing", "time_remaining")),
                "file": _known_fields(file_root, ("name", "display")),
            }))
            counters.append(status.get("telemetry_sequence"))
    except (ParseError, ValueError) as exc:
        raise ObservationError("malformed_or_unavailable") from exc
    counter_advanced = _strict_counter(counters)
    summary = _consistent(
        # The official Version schema's `printer` value is a software version,
        # not a printer model. The allowlisted PrusaLink endpoints do not expose
        # a documented hardware-model field, so identity must fail closed later.
        samples, observed_at, [], freshness_tokens, counter_advanced,
    )
    return ObservationSummary(
        summary.samples, summary.capabilities,
        firmware_version=safe_version(version.get("firmware")),
        api_version=safe_version(version.get("api")),
        freshness_seconds=summary.freshness_seconds,
        observed_model_family=summary.observed_model_family,
    )


def observe_elegoo(transport, *, clock: Callable[[], float] = time.monotonic) -> ObservationSummary:
    samples: list[ParsedSample] = []
    observed_at: list[float] = []
    model_hints: list[str | None] = []
    freshness_tokens: list[str] = []
    counters: list[object] = []
    device_topics: set[str] = set()
    firmware_versions: set[str] = set()
    protocol_versions: set[str] = set()
    try:
        for _ in range(MAX_ELEGOO_FRAMES):
            frame = transport.receive()
            if not isinstance(frame, (str, bytes)) or len(frame) > 262_144:
                raise ObservationError("payload_size_invalid")
            if not frame:
                raise ObservationError("passive_samples_unavailable")
            payload = decode_bounded_json_object(frame)
            topic = payload.get("Topic")
            if not isinstance(topic, str):
                raise ObservationError("malformed_or_unavailable")
            prefixes = ("sdcp/attributes/", "sdcp/status/", "sdcp/notice/")
            prefix = next((candidate for candidate in prefixes if topic.startswith(candidate)), None)
            if prefix is None or not topic[len(prefix):] or "/" in topic[len(prefix):]:
                raise ObservationError("malformed_or_unavailable")
            device_topics.add(topic[len(prefix):])
            if len(device_topics) != 1:
                raise ObservationError("target_identity_mismatch")

            if prefix == "sdcp/attributes/":
                attributes = payload.get("Attributes")
                if not isinstance(attributes, dict):
                    raise ObservationError("model_identity_unavailable")
                raw_model = attributes.get("MachineName")
                if not isinstance(raw_model, str):
                    raise ObservationError("model_identity_unavailable")
                model = normalize_model_name("elegoo", raw_model)
                if not model:
                    raise ObservationError("model_identity_unavailable")
                model_hints.append(model)
                firmware_version = _required_device_version(attributes.get("FirmwareVersion"))
                protocol_version = _required_device_version(attributes.get("ProtocolVersion"))
                firmware_versions.add(firmware_version)
                protocol_versions.add(protocol_version)
                if len({value.casefold() for value in model_hints if value}) != 1:
                    raise ObservationError("model_family_mismatch")
                if len(samples) == 2:
                    break
                continue

            samples.append(parse_elegoo_sample(payload))
            observed_at.append(clock())
            status = payload.get("Status") if isinstance(payload.get("Status"), dict) else {}
            print_info = status.get("PrintInfo") if isinstance(status.get("PrintInfo"), dict) else {}
            freshness_tokens.append(_freshness_token({
                "status": _known_fields(status, ("CurrentStatus",)),
                "print": _known_fields(print_info, (
                    "Status", "Progress", "CurrentLayer", "TotalLayer", "Filename",
                    "CurrentTicks", "TotalTicks",
                )),
            }))
            counters.append(print_info.get("CurrentTicks"))
            if len(samples) == 2 and model_hints:
                break
    except ObservationError:
        raise
    except (ParseError, PassivePolicyError, ValueError, TypeError) as exc:
        raise ObservationError("malformed_or_unavailable") from exc
    if not model_hints:
        raise ObservationError("model_identity_unavailable")
    if len(firmware_versions) != 1 or len(protocol_versions) != 1:
        raise ObservationError("device_version_mismatch")
    counter_advanced = _strict_counter(counters)
    summary = _consistent(
        samples, observed_at, model_hints, freshness_tokens, counter_advanced,
    )
    return ObservationSummary(
        summary.samples, summary.capabilities,
        firmware_version=next(iter(firmware_versions)),
        api_version=next(iter(protocol_versions)),
        freshness_seconds=summary.freshness_seconds,
        observed_model_family=summary.observed_model_family,
    )
