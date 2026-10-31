"""Route individual device sockets through their directly matching LAN."""
import socket
import time


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
