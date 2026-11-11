"""Real vendored ADB/FileSync frames over a short-writing TCP socket."""
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mobile/python'))
sys.path.insert(0,str(ROOT/'mobile/vendor/adb_shell-0.4.4-py3-none-any.whl'))
from android_host.adb import Driver, Registry
from android_host.network import write_all


class Upload(unittest.TestCase):
    def test_partial_sends_and_temporary_backpressure_never_duplicate_bytes(self):
        connection=Mock();attempts=[]
        results=[2,BlockingIOError(),1,3]
        def send(data):
            attempts.append(bytes(data));result=results.pop(0)
            if isinstance(result,Exception):raise result
            return result
        connection.send.side_effect=send
        with patch('android_host.network.select.select',return_value=([],[connection],[])):
            self.assertEqual(write_all(connection,b'abcdef',10),6)
        self.assertEqual(attempts,[b'abcdef',b'cdef',b'cdef',b'def'])

    def test_progress_cannot_extend_a_frame_deadline(self):
        connection=Mock();connection.send.return_value=1
        with patch('android_host.network.select.select',return_value=([],[connection],[])), \
             patch('android_host.network.time.monotonic',side_effect=[0,.1,.3,1.1]):
            with self.assertRaises(TimeoutError):write_all(connection,b'abcd',1)
        self.assertEqual([bytes(c.args[0]) for c in connection.send.call_args_list],[b'abcd',b'bcd'])

    def test_closed_or_unwritable_socket_fails_without_replaying(self):
        connection=Mock();connection.send.return_value=0
        with patch('android_host.network.select.select',return_value=([],[connection],[])):
            with self.assertRaises(ConnectionError):write_all(connection,b'payload',1)
        connection.send.assert_called_once()
        connection.reset_mock()
        with patch('android_host.network.select.select',return_value=([],[],[])):
            with self.assertRaises(TimeoutError):write_all(connection,b'payload',1)
        connection.send.assert_not_called()

    def test_large_filesync_upload_survives_tcp_short_writes_without_replaying(self):
        payload=bytes(range(256))*2169  # Same size class as the static resize helper.
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(3)
        address=server.getsockname();errors=[];received=bytearray();short=[];opens=[]
        def device():
            try:
                with server.accept()[0] as conn:
                    conn.settimeout(2)
                    def exact(count):
                        data=bytearray()
                        while len(data)<count:
                            part=conn.recv(count-len(data))
                            if not part:raise EOFError('incomplete ADB frame')
                            data.extend(part)
                        return bytes(data)
                    def read():
                        command,a,b,size,checksum,magic=struct.unpack('<6I',exact(24))
                        data=exact(size)
                        assert command^0xffffffff==magic and sum(data)&0xffffffff==checksum
                        return command.to_bytes(4,'little'),a,b,data
                    def send(command,a,b,data=b''):
                        value=int.from_bytes(command,'little')
                        conn.sendall(struct.pack('<6I',value,a,b,len(data),sum(data)&0xffffffff,value^0xffffffff)+data)
                    self.assertEqual(read()[0],b'CNXN')
                    send(b'CNXN',0x01000000,1<<20,b'device::ro.product.name=fixture;\0')
                    command,local,_,data=read();self.assertEqual(command,b'OPEN');opens.append(data)
                    self.assertEqual(data,b'sync:\0');send(b'OKAY',1,local)
                    pending=bytearray();done=False
                    while not done:
                        command,_,_,data=read();self.assertEqual(command,b'WRTE')
                        pending.extend(data);send(b'OKAY',1,local)
                        while len(pending)>=8:
                            kind,count=struct.unpack_from('<4sI',pending)
                            if kind==b'DONE':del pending[:8];done=True;break
                            if len(pending)<8+count:break
                            part=bytes(pending[8:8+count]);del pending[:8+count]
                            if kind==b'DATA':received.extend(part)
                            else:self.assertEqual(kind,b'SEND')
                    self.assertEqual(received,payload);self.assertEqual(pending,b'')
                    send(b'WRTE',1,local,struct.pack('<4sI',b'OKAY',0))
                    self.assertEqual(read()[0],b'OKAY')
                    self.assertEqual(read()[0],b'CLSE');send(b'CLSE',1,local)
            except BaseException as error:errors.append(error)
            finally:server.close()
        class ShortSocket:
            def __init__(self,connection):self.connection=connection
            def __getattr__(self,name):return getattr(self.connection,name)
            def send(self,data):
                if len(data)>8192:short.append(len(data))
                return self.connection.send(data[:8192])
        thread=threading.Thread(target=device,daemon=True);thread.start()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'helper';path.write_bytes(payload)
            registry=Registry(Mock(),temp);registry.tcp['fixture']=address
            def connect(endpoint,timeout,binder=None):return ShortSocket(socket.create_connection(endpoint,timeout=2))
            try:
                with patch('android_host.network.connect',side_effect=connect),patch.object(registry,'signer',return_value=Mock()):
                    context=Mock()
                    result=Driver(registry,'fixture').run(['push',str(path),'/data/local/tmp/helper'],timeout=.5,context=context)
                    self.assertEqual(result.returncode,0)
                    self.assertEqual(context.transfer.call_args_list[0].args[1:3],(0,len(payload)))
                    self.assertEqual(context.transfer.call_args_list[-1].args[1:3],(len(payload),len(payload)))
            finally:
                registry.close('fixture');thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertTrue(short);self.assertEqual(len(opens),1);self.assertEqual(received,payload)

    def test_upload_failure_keeps_progress_and_budget_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'f2fs-online-arm64';path.write_bytes(b'x'*100)
            registry=Registry(Mock(),tmp);client=Mock();registry.clients['fixture']=client
            def fail(*args,**kw):
                kw['progress_callback']('remote',40,100)
                raise TimeoutError('Reading timed out (888 seconds)')
            client.push.side_effect=fail;context=Mock()
            with self.assertRaisesRegex(ConnectionError,r'40/100 bytes; read_timeout_s=888'):
                Driver(registry,'fixture').run(['push',str(path),'/data/local/tmp/staged'],timeout=888,context=context)
            self.assertEqual(client.push.call_count,1)
            client.close.assert_called_once()
            self.assertEqual(context.transfer.call_args_list[-1].args[1:3],(40,100))
            self.assertNotIn('fixture',str(context.log.call_args_list))


if __name__=='__main__':unittest.main()
