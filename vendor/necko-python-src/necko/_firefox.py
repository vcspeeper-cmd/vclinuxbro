# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import copy
import ipaddress
import os
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from publicsuffixlist import PublicSuffixList
from requests.cookies import RequestsCookieJar, get_cookie_header
from requests.structures import CaseInsensitiveDict


def data_path(name):
    local = Path(__file__).with_name(name)
    if local.is_file():
        return local
    runtime = os.environ.get("NECKO_CORE_LIBRARY")
    if runtime:
        return Path(runtime).parent / "necko" / name
    checkout = Path(__file__).parents[3] / "obj-necko/necko" / name
    if checkout.is_file():
        return checkout
    return local


_suffix_file = data_path("firefox-suffixes.dat")
if _suffix_file.is_file():
    with _suffix_file.open(encoding="utf-8") as _suffix_data:
        _SUFFIXES = PublicSuffixList(_suffix_data)
else:
    _SUFFIXES = PublicSuffixList()
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
)
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def origin(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return f"{parsed.scheme}://{host}"


def site(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme, _SUFFIXES.privatesuffix(host) or host


def relation(source, target):
    if not source:
        return "none"
    if origin(source) == origin(target):
        return "same-origin"
    return "same-site" if site(source) == site(target) else "cross-site"


def trustworthy(url):
    parsed = urlsplit(url)
    if parsed.scheme == "https" or parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def default_headers(context):
    return CaseInsensitiveDict([
        ("User-Agent", USER_AGENT),
        ("Accept", ACCEPT if context == "navigation" else "*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Accept-Encoding", "gzip, deflate, br, zstd"),
        ("Connection", "keep-alive"),
    ])


def apply_headers(request, context, document_url, user_activation=True):
    headers = request.headers
    if not hasattr(request, "_firefox_explicit"):
        request._firefox_explicit = set(headers)
    explicit = request._firefox_explicit
    if headers.get("Accept-Encoding") == "gzip, deflate, br, zstd" and not trustworthy(
        request.url
    ):
        headers["Accept-Encoding"] = "gzip, deflate"
    reference = document_url or headers.get("Referer")
    relationship = relation(reference, request.url)
    computed = {}
    if context == "navigation":
        computed["Upgrade-Insecure-Requests"] = "1"
    if trustworthy(request.url):
        computed.update({
            "Sec-Fetch-Dest": "document" if context == "navigation" else "empty",
            "Sec-Fetch-Mode": "navigate" if context == "navigation" else "cors",
            "Sec-Fetch-Site": relationship,
        })
        if context == "navigation" and user_activation:
            computed["Sec-Fetch-User"] = "?1"
    computed["Priority"] = "u=0, i" if context == "navigation" else "u=4"
    if reference:
        secure_downgrade = (
            urlsplit(reference).scheme == "https"
            and urlsplit(request.url).scheme != "https"
        )
        if not secure_downgrade:
            parsed = urlsplit(reference)
            computed["Referer"] = (
                urlunsplit((
                    parsed.scheme,
                    urlsplit(origin(reference)).netloc,
                    parsed.path or "/",
                    parsed.query,
                    "",
                ))
                if relationship == "same-origin"
                else origin(reference) + "/"
            )
        if (
            request.method not in ("GET", "HEAD")
            or context == "fetch"
            and relationship != "same-origin"
        ):
            computed["Origin"] = origin(reference)
    elif request.method not in ("GET", "HEAD"):
        computed["Origin"] = "null"
    for name in (
        "Sec-Fetch-Dest",
        "Sec-Fetch-Mode",
        "Sec-Fetch-Site",
        "Sec-Fetch-User",
        "Upgrade-Insecure-Requests",
        "Priority",
        "Referer",
        "Origin",
    ):
        if name not in explicit:
            headers.pop(name, None)
            if name in computed:
                headers[name] = computed[name]
    request._firefox_context = context
    request._firefox_document = reference


def wire_headers(headers, http2, method="GET"):
    from ruyijs_necko.wire_fields import WireFields
    multiple = isinstance(headers, WireFields)
    ordered = WireFields() if multiple else CaseInsensitiveDict()
    standard = ("Host", "User-Agent", "Accept", "Accept-Language", "Accept-Encoding")
    late = ("Sec-Fetch-Storage-Access", "Origin", "Connection", "Referer", "Cookie", "Alt-Used",
            "Upgrade-Insecure-Requests", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site",
            "Sec-Fetch-User", "Priority", "Pragma", "If-Modified-Since", "If-None-Match",
            "Cache-Control", "Content-Length")
    if method not in ("GET", "HEAD"):
        late = ("Referer",) + tuple(name for name in late if name != "Referer")
    placed = {name.lower() for name in standard + late}
    page = []
    for name in headers.keys():
        if name.lower() not in placed and name.lower() not in [p.lower() for p in page]:
            page.append(name)
    first = standard + tuple(page) + late
    for name in first:
        if name in headers:
            if multiple:
                for key, value in headers.items():
                    if key.lower() == name.lower():
                        ordered.append(key, value)
            else:
                ordered[name] = headers[name]
    first_names = {key.lower() for key in first} if multiple else ()
    for name, value in headers.items():
        if multiple:
            if name.lower() not in first_names:
                ordered.append(name, value)
        elif name not in ordered:
            ordered[name] = value
    if http2:
        ordered["TE"] = "trailers"
    return ordered


class CookiePolicy(DefaultCookiePolicy):
    def __init__(self, context="navigation", document_url=None):
        super().__init__(strict_ns_domain=self.DomainStrictNonDomain)
        self.context = context
        self.document_url = document_url

    def set_ok(self, cookie, request):
        if not super().set_ok(cookie, request):
            return False
        domain = cookie.domain.lstrip(".")
        if cookie.domain_specified and _SUFFIXES.publicsuffix(domain) == domain:
            return False
        if cookie.secure and not trustworthy(request.get_full_url()):
            return False
        if cookie.name.startswith("__Secure-") and not cookie.secure:
            return False
        if cookie.name.startswith("__Host-") and (
            not cookie.secure or cookie.domain_specified or cookie.path != "/"
        ):
            return False
        same_site = self.same_site(cookie)
        return same_site != "none" or cookie.secure

    def return_ok(self, cookie, request):
        if not super().return_ok(cookie, request):
            return False
        if not self.document_url or site(self.document_url) == site(
            request.get_full_url()
        ):
            return True
        same_site = self.same_site(cookie)
        if same_site == "none":
            return cookie.secure
        return (
            same_site != "strict"
            and self.context == "navigation"
            and (
                request.get_method()
                if hasattr(request, "get_method")
                else request._r.method
            )
            in ("GET", "HEAD", "OPTIONS", "TRACE")
        )

    @staticmethod
    def same_site(cookie):
        return str(
            next(
                (
                    value
                    for key, value in cookie._rest.items()
                    if key.lower() == "samesite"
                ),
                "Lax",
            )
        ).lower()


def apply_cookies(request, context, document_url):
    jar = getattr(request, "_cookies", None)
    if jar is not None:
        for cookie in list(jar):
            if not cookie.domain:
                bound = copy.copy(cookie)
                bound.domain = urlsplit(request.url).hostname
                if "." not in bound.domain and ":" not in bound.domain:
                    bound.domain += ".local"
                jar.clear(cookie.domain, cookie.path, cookie.name)
                jar.set_cookie(bound)
        jar.set_policy(CookiePolicy(context, document_url))
        if getattr(request, "_firefox_cookie_header", False):
            request.headers.pop("Cookie", None)
            value = get_cookie_header(jar, request)
            if value:
                request.headers["Cookie"] = value


def cookie_jar(context, document_url):
    return RequestsCookieJar(policy=CookiePolicy(context, document_url))
