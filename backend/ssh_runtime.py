"""Load local SSH dependencies before any device-changing installation step."""


class HostRuntimeError(RuntimeError):
    pass


def load_ssh():
    try:
        import paramiko
        # Exercise the native cipher loader as well as Python imports. A frozen
        # backend can start successfully while its later DLL loads still fail.
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(bytes(32)), modes.CTR(bytes(16))).encryptor()
        cipher.update(bytes(16))
        cipher.finalize()
        return paramiko
    except (ImportError, OSError) as error:
        raise HostRuntimeError(
            'Local SSH runtime could not load. Extract the complete latest installer '
            'to a short folder and retry; no device repair is required for this error. / '
            '控制端 SSH 依赖加载失败。请将最新版安装包完整解压到较短目录后重试；'
            '这条错误不表示设备损坏。\n' + str(error)
        ) from error
