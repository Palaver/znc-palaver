import time
import os
import asyncio

import pytest
from semantic_version import Version


# All test coroutines will be treated as marked.
pytestmark = pytest.mark.asyncio


async def get_znc_version():
    proc = await asyncio.create_subprocess_shell(
        'znc --version',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)

    stdout, stderr = await proc.communicate()
    return stdout.decode('utf-8').split()[1]


async def requires_znc_version(znc_version):
    actual_version = await get_znc_version()

    if Version(znc_version) > Version(actual_version):
        pytest.skip('ZNC >= {} is required for this test, found {}'.format(znc_version, version))


async def assert_user_agent(header):
    header = header.decode('utf-8')
    assert header.endswith('\r\n')

    (name, value) = header.strip().split(': ')
    assert name == 'User-Agent'

    products = value.split(' ')
    assert len(products) == 2

    product1, product1_version = products[0].split('/')
    assert product1 == 'znc-palaver'
    assert Version(product1_version).major >= 1

    product2, product2_version = products[1].split('/')
    assert product2 == 'znc'
    assert product2_version == await get_znc_version()


async def setUp(event_loop):
    running_as_root = os.getuid() == 0
    allow_root = ' --allow-root' if running_as_root else ''

    proc = await asyncio.create_subprocess_shell(f'znc -d test/fixtures --foreground --debug{allow_root}', loop=event_loop)
    time.sleep(31 if running_as_root else 1)

    (reader, writer) = await asyncio.open_connection('localhost', 6698, loop=event_loop)
    writer.write(b'CAP LS 302\r\n')

    line = await reader.readline()
    writer.write(b'CAP REQ :palaverapp.com\r\n')

    line = await reader.readline()
    assert line == b':irc.znc.in CAP unknown-nick ACK :palaverapp.com\r\n'

    writer.write(b'CAP END\r\n')
    writer.write(b'PASS admin\r\n')
    writer.write(b'USER admin admin admin\r\n')
    writer.write(b'NICK admin\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b':irc.znc.in 001 admin :Welcome to ZNC\r\n'

    return (proc, reader, writer)


async def tearDown(proc):
    await asyncio.sleep(0.2)

    proc.kill()
    await proc.wait()

    config = 'test/fixtures/moddata/palaver/palaver.conf'
    if os.path.exists(config):
        os.remove(config)


async def test_registering_device(event_loop):
    (proc, reader, writer) = await setUp(event_loop)

    writer.write(b'PALAVER IDENTIFY 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e b758eaab1a4611a310642a6e8419fbff\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b'PALAVER REQ *\r\n'

    writer.write(b'PALAVER BEGIN 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e\r\n')
    writer.write(b'PALAVER SET PUSH-TOKEN 605b64f5addc408fcfa7ff0685e2d065fdecb127\r\n')
    writer.write(b'PALAVER SET PUSH-ENDPOINT https://api.palaverapp.com/1/push\r\n')
    writer.write(b'PALAVER ADD MENTION-KEYWORD cocode\r\n')
    writer.write(b'PALAVER ADD MENTION-KEYWORD {nick}\r\n')
    writer.write(b'PALAVER END\r\n')
    await writer.drain()

    await tearDown(proc)


async def test_loading_module_new_cap(event_loop):
    await requires_znc_version('1.7.0')

    (proc, reader, writer) = await setUp(event_loop)

    writer.write(b'PRIVMSG *status :unloadmod palaver\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b':irc.znc.in CAP admin DEL :palaverapp.com\r\n'

    line = await reader.readline()
    assert line == b':*status!znc@znc.in PRIVMSG admin :Module palaver unloaded.\r\n'

    writer.write(b'PRIVMSG *status :loadmod palaver\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b':irc.znc.in CAP admin NEW :palaverapp.com\r\n'

    line = await reader.readline()
    assert line == b':*status!znc@znc.in PRIVMSG admin :Loaded module palaver: [test/fixtures/modules/palaver.so]\r\n'

    await tearDown(proc)


async def test_unloading_module_del_cap(event_loop):
    await requires_znc_version('1.7.0')

    (proc, reader, writer) = await setUp(event_loop)

    writer.write(b'PRIVMSG *status :unloadmod palaver\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b':irc.znc.in CAP admin DEL :palaverapp.com\r\n'

    line = await reader.readline()
    assert line == b':*status!znc@znc.in PRIVMSG admin :Module palaver unloaded.\r\n'

    await tearDown(proc)


async def test_receiving_notification(event_loop):
    (proc, reader, writer) = await setUp(event_loop)

    async def connected(reader, writer):
        line = await reader.readline()
        assert line == b'POST /push HTTP/1.1\r\n'

        line = await reader.readline()
        assert line == b'Host: 127.0.0.1\r\n'

        line = await reader.readline()
        assert line == b'Authorization: Bearer 9167e47b01598af7423e2ecd3d0a3ec4\r\n'

        line = await reader.readline()
        assert line == b'Connection: close\r\n'

        line = await reader.readline()
        assert line == b'Content-Length: 109\r\n'

        line = await reader.readline()
        assert line == b'Content-Type: application/json\r\n'

        await assert_user_agent(await reader.readline())

        line = await reader.readline()
        assert line == b'\r\n'

        line = await reader.readline()
        assert line == b'{"badge": 1,"message": "Test notification","sender": "palaver","network": "b758eaab1a4611a310642a6e8419fbff"}'

        writer.write(b'HTTP/1.1 204 No Content\r\n\r\n')
        await writer.drain()
        writer.close()

        connected.called = True

    server = await asyncio.start_server(connected, host='127.0.0.1', port=0, loop=event_loop)
    await asyncio.sleep(0.2)
    addr = server.sockets[0].getsockname()
    url = f'Serving on http://{addr[0]}:{addr[1]}/push'

    writer.write(b'PALAVER IDENTIFY 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e b758eaab1a4611a310642a6e8419fbff\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b'PALAVER REQ *\r\n'

    writer.write(b'PALAVER BEGIN 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e\r\n')
    writer.write(f'PALAVER SET PUSH-ENDPOINT {url}\r\n'.encode('utf-8'))
    writer.write(b'PALAVER END\r\n')
    await writer.drain()

    writer.write(b'PRIVMSG *palaver :test\r\n')
    await writer.drain()

    await asyncio.sleep(0.2)
    server.close()
    await server.wait_closed()

    await tearDown(proc)

    assert connected.called


async def test_receiving_notification_with_push_token(event_loop):
    (proc, reader, writer) = await setUp(event_loop)

    async def connected(reader, writer):
        line = await reader.readline()
        assert line == b'POST /push HTTP/1.1\r\n'

        line = await reader.readline()
        assert line == b'Host: 127.0.0.1\r\n'

        line = await reader.readline()
        assert line == b'Authorization: Bearer abcdefg\r\n'

        line = await reader.readline()
        assert line == b'Connection: close\r\n'

        line = await reader.readline()
        assert line == b'Content-Length: 109\r\n'

        line = await reader.readline()
        assert line == b'Content-Type: application/json\r\n'

        await assert_user_agent(await reader.readline())

        line = await reader.readline()
        assert line == b'\r\n'

        line = await reader.readline()
        assert line == b'{"badge": 1,"message": "Test notification","sender": "palaver","network": "b758eaab1a4611a310642a6e8419fbff"}'

        writer.write(b'HTTP/1.1 204 No Content\r\n\r\n')
        await writer.drain()
        writer.close()

        connected.called = True

    server = await asyncio.start_server(connected, host='127.0.0.1', port=0, loop=event_loop)
    await asyncio.sleep(0.2)
    addr = server.sockets[0].getsockname()
    url = f'Serving on http://{addr[0]}:{addr[1]}/push'

    writer.write(b'PALAVER IDENTIFY 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e b758eaab1a4611a310642a6e8419fbff\r\n')
    await writer.drain()

    line = await reader.readline()
    assert line == b'PALAVER REQ *\r\n'

    writer.write(b'PALAVER BEGIN 9167e47b01598af7423e2ecd3d0a3ec4 611d3a30a3d666fc491cdea0d2e1dd6e\r\n')
    writer.write(f'PALAVER SET PUSH-ENDPOINT {url}\r\n'.encode('utf-8'))
    writer.write(f'PALAVER SET PUSH-TOKEN abcdefg\r\n'.encode('utf-8'))
    writer.write(b'PALAVER END\r\n')
    await writer.drain()

    writer.write(b'PRIVMSG *palaver :test\r\n')
    await writer.drain()

    await asyncio.sleep(0.2)
    server.close()
    await server.wait_closed()

    await tearDown(proc)

    assert connected.called
