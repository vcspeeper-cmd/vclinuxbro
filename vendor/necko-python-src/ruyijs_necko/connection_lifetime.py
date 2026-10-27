"""Attach physical leases to the original TCP/H3 creation and final cleanup."""


def _claim(connection, token):
    if token is not None:
        token.claim()
    try:
        connection._ruyijs_token = token
    except BaseException:
        if token is not None:
            token.release()
        raise


def native_initialize(connection, initialize, arguments, token):
    _claim(connection, token)
    try:
        if token is not None and arguments[-1] is not None:
            token.bind(arguments[-1].cancel)
        initialize(*arguments)
        if token is not None:
            token.bind(connection.interrupt)
            token.opened()
    except BaseException:
        if getattr(connection, "_handle_lock", None) is not None:
            connection.close()
        else:
            native_closed(connection)
        raise


def native_closed(connection):
    token = getattr(connection, "_ruyijs_token", None)
    if token is not None:
        token.release()
        connection._ruyijs_token = None


def h3_initialize(connection, initialize, arguments, token):
    _claim(connection, token)
    try:
        connection._ruyijs_retained_udp = None
        if token is not None and arguments[-2] is not None:
            token.bind(arguments[-2].cancel)
        initialize(*arguments)
        if token is not None:
            token.bind(connection._cancel_connect)
            token.opened()
    except BaseException:
        thread = getattr(connection, "_thread", None)
        if thread is not None and thread.ident is not None:
            connection.close()
        else:
            h3_finished(connection, None)
        raise


def h3_finished(connection, udp):
    error = None
    if udp is not None:
        connection._ruyijs_retained_udp = udp
    udp = getattr(connection, "_ruyijs_retained_udp", None)
    if getattr(connection, "handle", 0):
        try:
            connection._call("close")
            connection.handle = 0
        except BaseException as caught:
            # Neqo borrows this UDP handle until its close is confirmed.
            connection.error = getattr(connection, "error", None) or _cleanup_error(caught)
            token = getattr(connection, "_ruyijs_token", None)
            if token is not None:
                token.closing()
                token.bind(connection._cancel_connect)
            return
    for attribute in ("_ruyijs_retained_udp", "_wake_read", "_wake_write", "_dll_directory"):
        resource = getattr(connection, attribute, None)
        if resource is not None:
            try:
                resource.close()
                setattr(connection, attribute, None)
            except BaseException as caught:
                error = error or _cleanup_error(caught)
    token = getattr(connection, "_ruyijs_token", None)
    if error is not None:
        connection.error = getattr(connection, "error", None) or error
        if token is not None:
            token.closing()
            token.bind(connection._cancel_connect)
    elif token is not None:
        token.release()
        connection._ruyijs_token = None


def _cleanup_error(error):
    # Internal cleanup diagnostics must not retain old call frames/resources.
    # The original type/message remain, while explicit fields own live handles.
    error.__traceback__ = error.__context__ = error.__cause__ = None
    return error
