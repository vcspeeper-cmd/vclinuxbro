# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from . import sessions


def request(method, url, **kwargs):
    with sessions.Session() as session:
        return session.request(method=method, url=url, **kwargs)


def get(url, params=None, **kwargs):
    return request("get", url, params=params, **kwargs)


def options(url, **kwargs):
    kwargs.setdefault("allow_redirects", True)
    return request("options", url, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault("allow_redirects", False)
    return request("head", url, **kwargs)


def post(url, data=None, json=None, **kwargs):
    return request("post", url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    return request("put", url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    return request("patch", url, data=data, **kwargs)


def delete(url, **kwargs):
    return request("delete", url, **kwargs)
