"""W1 closed payload schema/direction; no sequence, registry or credit ledger."""
from .worker_json import _envelope, _tree, decode_control_frame
from .worker_schema_types import (REASONS, VERSIONS, boolean, bounded, choice,
    hexadecimal, identity, integer, message, method, nullable, object_fields, true,
    url, watermark)
from .worker_schema_values import error, headers, key, options, tls

BODY = bounded(0, 16 * 1024 * 1024)
REASON = choice(*REASONS)
ZERO = bounded(0, 0)
SCHEMAS = {
    "hello": ("both", dict(role=choice("parent", "worker"), pid=bounded(1),
        nonce=hexadecimal(32, nonzero=True), manifest=hexadecimal(64), qpcFrequency=identity)),
    "ready": ("worker", dict(manifest=hexadecimal(64), abi=bounded(1, 1),
        nss=choice("3.130"), nspr=choice("4.40"), wireProfile=choice("firefox158"))),
    "reserve": ("parent", dict(reservationId=identity, partition=bounded(0, 3),
        deadlineQpc=identity, qpcFrequency=identity, method=method, url=url, headers=headers,
        uploadLength=BODY, replayable=true, responseLimit=BODY, transportOptions=options)),
    "accepted": ("worker", dict(reservationId=identity, state=choice("reserved"))),
    "activate": ("parent", dict(reservationId=identity, key=key)),
    "activated": ("worker", dict(reservationId=identity, key=key, state=choice("queued", "active"))),
    "release-reservation": ("parent", dict(reservationId=identity, reason=REASON)),
    "reservationReleased": ("worker", dict(reservationId=identity, reason=REASON)),
    "ack-reservation": ("parent", dict(reservationId=identity)),
    "upload-credit": ("worker", dict(key=key, grantThrough=watermark, ackedThrough=watermark, stop=boolean)),
    "download-credit": ("parent", dict(key=key, grantThrough=watermark, ackedThrough=watermark, stop=boolean)),
    "upload-end": ("parent", dict(key=key, lastSeq=watermark, totalBytes=BODY)),
    "headers": ("worker", dict(key=key, status=bounded(100, 599), version=choice(*VERSIONS),
        orderedHeaders=headers, tls=nullable(tls), attempt=bounded(1, 3),
        bodyEncoding=choice("decoded"), headerRepresentation=choice("encoded"))),
    "body-end": ("worker", dict(key=key, lastSeq=watermark, encodedBytes=BODY, decodedBytes=BODY)),
    "cancel": ("parent", dict(key=key, reason=REASON, message=message, lastUploadSeq=watermark)),
    "terminal": ("worker", dict(key=key, outcome=choice("complete", "error", "cancelled"),
        error=nullable(error), lastDownloadSeq=watermark)),
    "released": ("worker", dict(key=key, leaseClosed=true, interrupts=ZERO,
        localResourcesReleased=true, lastUploadAck=watermark, lastDownloadSeq=watermark)),
    "ack-terminal": ("parent", dict(key=key, disposition=choice("accepted", "discarded"))),
    "close": ("parent", dict(reason=REASON)),
    "closed": ("worker", dict(tickets=ZERO, active=ZERO, queued=ZERO, connections=ZERO,
        resolverActive=ZERO, credits=ZERO, spools=ZERO)),
    "fatal": ("both", dict(error=error)),
}


def validate_payload(envelope, sender):
    """Validate a W0 envelope and return the same DTO without mutation or copying."""
    _envelope(envelope)
    _tree(envelope)
    if type(sender) is not str or sender not in ("parent", "worker"):
        raise ValueError("Worker schema sender")
    kind, payload = envelope["type"], envelope["payload"]
    direction, fields = SCHEMAS[kind]
    if direction != "both" and direction != sender:
        raise ValueError("Worker schema direction")
    object_fields(payload, fields)
    if kind == "hello" and payload["role"] != sender:
        raise ValueError("Worker hello role differs from sender")
    if kind == "terminal":
        outcome, failure = payload["outcome"], payload["error"]
        if (outcome == "complete") != (failure is None):
            raise ValueError("Worker terminal error/outcome mismatch")
        if outcome == "cancelled" and failure["kind"] not in ("Cancelled", "DeadlineExceeded"):
            raise ValueError("Worker terminal cancellation error kind")
    return envelope


def decode_message(frame, sender):
    return validate_payload(decode_control_frame(frame), sender)
