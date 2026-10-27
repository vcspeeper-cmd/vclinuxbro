# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Requests-style API with standalone NSS and Firefox transport defaults."""

import sys

from requests import (
    ConnectionError,
    ConnectTimeout,
    FileModeWarning,
    HTTPError,
    JSONDecodeError,
    PreparedRequest,
    ReadTimeout,
    Request,
    RequestException,
    Response,
    Timeout,
    TooManyRedirects,
    URLRequired,
    auth,
    certs,
    codes,
    cookies,
    exceptions,
    hooks,
    models,
    packages,
    status_codes,
    structures,
    utils,
)

from . import adapters, api, sessions
from ._transport import HeaderMap
from .api import delete, get, head, options, patch, post, put, request
from .sessions import Session, session

__version__ = "0.8.1"
NeckoError = RequestException

for _module in (
    auth,
    certs,
    cookies,
    exceptions,
    hooks,
    models,
    packages,
    status_codes,
    structures,
    utils,
):
    sys.modules[f"{__name__}.{_module.__name__.rsplit('.', 1)[-1]}"] = _module

__all__ = [
    "Session",
    "session",
    "Request",
    "PreparedRequest",
    "Response",
    "request",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "codes",
    "RequestException",
    "ConnectionError",
    "ConnectTimeout",
    "HTTPError",
    "ReadTimeout",
    "Timeout",
    "TooManyRedirects",
    "URLRequired",
    "JSONDecodeError",
    "FileModeWarning",
    "NeckoError",
    "HeaderMap",
    "exceptions",
    "auth",
    "cookies",
    "adapters",
    "api",
    "sessions",
    "models",
    "structures",
    "utils",
    "certs",
    "hooks",
    "packages",
    "status_codes",
]
