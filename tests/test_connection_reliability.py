"""Quiet ADB commands, per-socket LAN routing and bounded SSH channels."""
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE/'mobile/python'))
from android_host.adb import Registry,Driver
from android_host.network import connect
from backend import host
from backend.ssh_runtime import connect_ssh
from backend.ota_desktop import collect,execute,send


class Connections(unittest.TestCase):
    def test_heartbeat_split_at_any_packet_boundary_is_not_command_output(self):
        for stride in (1,3,31,59,123):
            with self.subTest(stride=stride),tempfile.TemporaryDirectory() as d:
                client=Mock();events=Mock();registry=Registry(Mock(),d)
                registry.clients['tcp:fixture']=client
                def stream(command,**kw):
                    self.assertEqual((kw['transport_timeout_s'],kw['read_timeout_s']),(30,30))
                    marker=re.search('__OPENWRT_WAIT_[a-f0-9]+__',command)[0].encode()
                    final=re.search('__OPENWRT_EXIT_[a-f0-9]+__',command)[0].encode()
                    raw=b'first\x00'+b'\n'+marker+b'\n'+b'last\n'+marker+b'\n\n'+final+b'7\n'
                    return (raw[i:i+stride] for i in range(0,len(raw),stride))
                client.streaming_shell.side_effect=stream
                result=Driver(registry,'tcp:fixture').run(['shell','long-command'],timeout=600,context=events)
                self.assertEqual(result.returncode,7)
                self.assertEqual(result.stdout,b'first\x00last')
                self.assertEqual(events.log.call_count,2)

    def test_heartbeat_does_not_reset_the_overall_command_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            client=Mock();registry=Registry(Mock(),d);registry.clients['fixture']=client
            def stream(command,**kw):
                marker=re.search('__OPENWRT_WAIT_[a-f0-9]+__',command)[0]
                yield ('\n'+marker+'\n').encode()
            client.streaming_shell.side_effect=stream
            with patch('android_host.adb.time.monotonic',side_effect=[0,601]):
                with self.assertRaises(subprocess.TimeoutExpired):
                    Driver(registry,'fixture').run(['shell','long-command'],timeout=600)
            client.close.assert_called_once()
            self.assertNotIn('fixture',registry.clients)

    @unittest.skipUnless(os.name=='posix','POSIX shell fixture')
    def test_real_quiet_command_reports_progress_and_reaps_heartbeat_without_delay(self):
        with tempfile.TemporaryDirectory() as d:
            client=Mock();registry=Registry(Mock(),d);registry.clients['fixture']=client
            events=[];context=Mock();context.log.side_effect=lambda *a,**k:events.append(time.monotonic())
            children=[]
            def stream(command,**kw):
                p=subprocess.Popen(['sh','-c',command],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                children.append(p)
                while b:=p.stdout.read(1):yield b
                self.assertEqual(p.wait(timeout=2),0)
                self.assertEqual(p.stderr.read(),b'')
            client.streaming_shell.side_effect=stream
            start=time.monotonic()
            try:
                result=Driver(registry,'fixture').run(['shell','printf first; sleep 6; printf last; exit 7'],timeout=600,context=context)
                self.assertEqual((result.returncode,result.stdout),(7,b'firstlast'))
                self.assertTrue(events)
                self.assertLess(events[0]-start,5.8)
                self.assertLess(time.monotonic()-start,8)
            finally:
                for p in children:
                    if p.poll() is None:p.kill();p.wait()

    def test_mobile_socket_is_bound_before_connect_and_failed_addresses_are_closed(self):
        first,second=Mock(),Mock();first.fileno.return_value=10;second.fileno.return_value=11
        first.connect.side_effect=OSError('first address unavailable')
        order=[]
        second.connect.side_effect=lambda endpoint:order.append(('connect',endpoint))
        binder=lambda fd,address:order.append(('bind',fd,address))
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('192.168.77.1',22)),
                   (socket.AF_INET6,socket.SOCK_STREAM,0,'',('fd00::1',22,0,0))]
        with patch('android_host.network.socket.getaddrinfo',return_value=addresses), \
             patch('android_host.network.socket.socket',side_effect=[first,second]):
            self.assertIs(connect(('router',22),30,binder),second)
        self.assertEqual(order,[('bind',10,'192.168.77.1'),('bind',11,'fd00::1'),('connect',('fd00::1',22,0,0))])
        first.close.assert_called_once();second.close.assert_not_called()

    def test_native_network_binding_error_cannot_leak_a_socket(self):
        connection=Mock()
        with patch('android_host.network.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('192.168.77.1',22))]), \
             patch('android_host.network.socket.socket',return_value=connection):
            with self.assertRaisesRegex(RuntimeError,'bind failure'):
                connect(('192.168.77.1',22),30,Mock(side_effect=RuntimeError('bind failure')))
        connection.close.assert_called_once();connection.connect.assert_not_called()

    def test_ssh_uses_mobile_lan_socket_and_keepalive_without_replaying_commands(self):
        client=Mock();provider=Mock();connection=provider.tcp_connection.return_value
        with patch.object(host,'_provider',provider):connect_ssh(client,'192.168.77.1','fixture')
        provider.tcp_connection.assert_called_once_with(('192.168.77.1',22),30)
        options=client.connect.call_args.kwargs
        self.assertIs(options['sock'],connection)
        self.assertEqual((options['banner_timeout'],options['auth_timeout'],options['channel_timeout']),(60,60,60))
        client.get_transport().set_keepalive.assert_called_once_with(15)
        client.exec_command.assert_not_called()
        client.connect.side_effect=socket.timeout('handshake timeout')
        with patch.object(host,'_provider',provider),self.assertRaises(socket.timeout):
            connect_ssh(client,'192.168.77.1','fixture')
        connection.close.assert_called_once();client.close.assert_called_once()

    def test_ssh_channel_close_and_missing_exit_status_are_failures(self):
        channel=Mock();channel.exit_status_ready.return_value=True
        channel.recv_ready.return_value=False;channel.recv_stderr_ready.return_value=False
        channel.recv_exit_status.return_value=-1
        with self.assertRaisesRegex(ConnectionError,'completion'):collect(channel)
        channel.close.assert_called_once()
        client=Mock();client.get_transport().open_session.return_value=channel
        channel.exec_command.side_effect=socket.timeout('request timed out')
        with self.assertRaises(socket.timeout):execute(client,'write-command')
        self.assertEqual(channel.exec_command.call_count,1)

    def test_upload_completion_is_bounded_and_cannot_silently_hang(self):
        client=Mock();channel=client.get_transport().open_session.return_value
        channel.exit_status_ready.return_value=False;channel.recv_ready.return_value=False
        channel.recv_stderr_ready.return_value=False
        with patch('backend.ota_desktop.time.monotonic',side_effect=[0,121]):
            with self.assertRaises(TimeoutError):send(client,'cat > fixture',[b'one',b'two'])
        channel.close.assert_called()
        self.assertEqual(channel.exec_command.call_count,1)


if __name__=='__main__':unittest.main()
