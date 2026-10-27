# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from urllib3.exceptions import ProtocolError

MAX_REQUEST_ATTEMPTS = 10


class Reset(ProtocolError):
    pass


class Rejected(Reset):
    pass


class Http1Required(Reset):
    pass


class DirectRouteReset(Reset):
    pass


def restartable(error, method, exchange, reused):
    received = getattr(
        exchange, "received_data", getattr(error, "_necko_received", False)
    )
    if received:
        return False
    if isinstance(error, Rejected):
        return True
    sent = getattr(exchange, "sent_data", getattr(error, "_necko_sent", True))
    return isinstance(error, Reset) and (
        method in {"GET", "HEAD", "OPTIONS", "TRACE", "PROPFIND", "REPORT", "SEARCH"}
        or not sent
        or reused
    )
