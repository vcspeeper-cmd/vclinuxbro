# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from requests.sessions import Session as RequestsSession

from . import _firefox
from ._transport import HTTPAdapter


class Session(RequestsSession):
    __attrs__ = RequestsSession.__attrs__ + [
        "timeout",
        "context",
        "document_url",
        "user_activation",
    ]

    def __init__(
        self,
        library_path=None,
        headers=None,
        timeout=None,
        verify=True,
        auth=None,
        context="navigation",
        document_url=None,
        user_activation=True,
    ):
        super().__init__()
        if context not in ("navigation", "fetch"):
            raise ValueError("context must be navigation or fetch")
        self.context = context
        self.document_url = document_url
        self.user_activation = bool(user_activation)
        self.headers = _firefox.default_headers(context)
        self.cookies = _firefox.cookie_jar(context, document_url)
        for adapter in self.adapters.values():
            adapter.close()
        adapter = HTTPAdapter(library_path=library_path)
        self.mount("http://", adapter)
        self.mount("https://", adapter)
        self.timeout = timeout
        self.verify = verify
        self.auth = auth
        if headers:
            self.headers.update(headers)

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        prepared._firefox_cookie_header = (
            "Cookie" not in (request.headers or {}) and "Cookie" not in self.headers
        )
        _firefox.apply_headers(
            prepared, self.context, self.document_url, self.user_activation
        )
        _firefox.apply_cookies(prepared, self.context, self.document_url)
        return prepared

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        previous = response.request
        prepared_request._firefox_explicit = getattr(
            previous, "_firefox_explicit", set()
        )
        prepared_request._firefox_cookie_header = True
        _firefox.apply_headers(
            prepared_request, self.context, self.document_url, self.user_activation
        )
        _firefox.apply_cookies(prepared_request, self.context, self.document_url)

    def send(self, request, **kwargs):
        _firefox.apply_headers(
            request, self.context, self.document_url, self.user_activation
        )
        _firefox.apply_cookies(request, self.context, self.document_url)
        if kwargs.get("timeout") is None and self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


def session():
    return Session()
