"""Route individual device sockets through their directly matching LAN."""
import socket
import select
import time


def write_all(connection, data, timeout):
    """One complete ADB frame, including TCP short writes, under one deadline.

    adb-shell's IO manager does not consume bulk_write's returned byte count.
    A nonblocking socket therefore must not return with a partially sent frame.
    Never resend the prefix or restart the ADB/FileSync operation.
    """
    deadline=None if timeout is None else time.monotonic()+timeout
    remaining=memoryview(data)
    def stalled():
        return TimeoutError(f'ADB send timed out: {len(data)-len(remaining)}/{len(data)} frame bytes / ADB 发送超时')
    while remaining:
        budget=None if deadline is None else deadline-time.monotonic()
        if budget is not None and budget<=0:
            raise stalled()
        _,ready,_=select.select([], [connection], [], budget)
        if not ready:raise stalled()
        try:sent=connection.send(remaining)
        except (BlockingIOError,InterruptedError):continue
        if sent<=0:raise ConnectionError('ADB connection closed during send / ADB 发送过程中连接断开')
        remaining=remaining[sent:]
    return len(data)


def connect(address, timeout, bind_socket=None):
    if bind_socket is None:
        return socket.create_connection(address, timeout=timeout)
    deadline=time.monotonic()+timeout
    error=None
    for family,kind,proto,_,endpoint in socket.getaddrinfo(*address,0,socket.SOCK_STREAM):
        connection=socket.socket(family,kind,proto)
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Device TCP connection timed out / 设备 TCP 连接超时')
            connection.settimeout(remaining)
            # Native side binds only when Wi-Fi/Ethernet has an explicit
            # route to this address. It never binds the whole application.
            bind_socket(connection.fileno(),endpoint[0])
            connection.connect(endpoint)
            return connection
        except OSError as caught:
            connection.close();error=caught
        except BaseException:
            connection.close();raise
    if error is not None:raise error
    raise OSError('No device address resolved / 无法解析设备地址')
