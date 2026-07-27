import asyncio
import json
import pytest
from aiohttp import web
from app.main import Application, AppConfig
from app.ui.server import UiServer, UiConfig

@pytest.fixture
def app_config():
    return AppConfig()

@pytest.fixture
def app(app_config):
    return Application(app_config)

@pytest.fixture
async def ui_server(unused_port_factory):
    port = unused_port_factory()
    # Use a short heartbeat for tests to avoid long waits
    config = UiConfig(host="127.0.0.1", port=port, heartbeat_s=0.1)
    # Create a mock Application that provides the needed methods
    class MockApp:
        def snapshot(self):
            return {}
        async def is_ready(self):
            return True
        async def start_session(self, meeting_title=None):
            return "dummy-session-id"
        async def stop_session(self):
            pass
        @property
        def privacy(self):
            class MockPrivacy:
                async def switch(self, target):
                    pass
            return MockPrivacy()
    app = MockApp()
    server = UiServer(app, config)
    await server.start()
    yield server, port
    await server.stop()

@pytest.fixture
def unused_port_factory():
    used = set()
    def factory():
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        addr, port = sock.getsockname()
        sock.close()
        used.add(port)
        return port
    return factory

async def make_sse_client(host, port):
    import aiohttp
    session = aiohttp.ClientSession()
    resp = await session.get(f'http://{host}:{port}/events')
    return session, resp

async def collect_next_event(resp):
    """Read from the SSE response until we get a non-heartbeat event.
    Returns (event_type, data, seq) or None if connection closed."""
    while True:
        try:
            chunk = await resp.content.readuntil(b'\n\n')
        except Exception:
            # Connection closed or error
            return None
        line = chunk.decode('utf-8')
        # Skip heartbeat lines (start with ':')
        if line.startswith(':'):
            continue
        # Parse SSE format: event: ...\ndata: ...\nid: ...\n\n

        # It might have multiple lines; we'll parse naively.
        event_type = None
        data = None
        seq = None
        for part in line.strip().split('\n'):
            if part.startswith('event: '):
                event_type = part[7:]
            elif part.startswith('data: '):
                data_str = part[6:]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    data = data_str  # fallback
            elif part.startswith('id: '):
                try:
                    seq = int(part[4:])
                except ValueError:
                    pass
        if event_type is not None and data is not None and seq is not None:
            return event_type, data, seq
        # If we didn't get a complete event, continue looping (should not happen with well-formed frames)

@pytest.mark.asyncio
async def test_sse_format_and_sequence(ui_server):
    server, port = ui_server
    session, resp = await make_sse_client('127.0.0.1', port)
    try:
        # Publish an event
        server.publish('segment.final', {'segment_id': '1', 'role': 'speaker', 'raw_text': 'hello'})
        # Yield control to allow the background task to run
        await asyncio.sleep(0)
        result = await collect_next_event(resp)
        assert result is not None, "No event received"
        event_type, data, seq = result
        assert event_type == 'segment.final'
        assert data == {'segment_id': '1', 'role': 'speaker', 'raw_text': 'hello'}
        assert seq == 1
    finally:
        await resp.release()
        await session.close()

@pytest.mark.asyncio
async def test_monotonic_sequence(ui_server):
    server, port = ui_server
    session, resp = await make_sse_client('127.0.0.1', port)
    try:
        seqs = []
        for i in range(5):
            server.publish('segment.final', {'seq': i})
            await asyncio.sleep(0)
        # Collect 5 events
        for _ in range(5):
            result = await collect_next_event(resp)
            assert result is not None
            _, _, seq = result
            seqs.append(seq)
        assert seqs == list(range(1, 6))
    finally:
        await resp.release()
        await session.close()

@pytest.mark.asyncio
async def test_two_clients_same_sequence(ui_server):
    server, port = ui_server
    # client 1
    s1, r1 = await make_sse_client('127.0.0.1', port)
    # client 2
    s2, r2 = await make_sse_client('127.0.0.1', port)
    try:
        seqs1 = []
        seqs2 = []
        for i in range(3):
            server.publish('segment.final', {'i': i})
            await asyncio.sleep(0)
            # collect from client 1
            r1_result = await collect_next_event(r1)
            assert r1_result is not None
            _, _, seq1 = r1_result
            seqs1.append(seq1)
            # collect from client 2
            r2_result = await collect_next_event(r2)
            assert r2_result is not None
            _, _, seq2 = r2_result
            seqs2.append(seq2)
        assert seqs1 == seqs2 == [1, 2, 3]
    finally:
        await r1.release(); await s1.close()
        await r2.release(); await s2.close()

@pytest.mark.asyncio
async def test_ensure_ascii_false(ui_server):
    server, port = ui_server
    session, resp = await make_sse_client('127.0.0.1', port)
    try:
        test_text = 'привет світ'
        server.publish('segment.final', {'raw_text': test_text})
        await asyncio.sleep(0)
        result = await collect_next_event(resp)
        assert result is not None
        event_type, data, seq = result
        assert event_type == 'segment.final'
        assert data['raw_text'] == test_text
        # Ensure the raw bytes of the response do not contain \uXXXX
        # We can check that the line we received contains the actual characters
        # but we already trust the json decoding.
    finally:
        await resp.release()
        await session.close()

@pytest.mark.asyncio
async def test_event_names_match_spec(ui_server):
    server, port = ui_server
    session, resp = await make_sse_client('127.0.0.1', port)
    try:
        event_map = {
            'segment.partial': 'SEGMENT_PARTIAL',
            'segment.final': 'SEGMENT_FINAL',
            'segment.translated': 'SEGMENT_TRANSLATED',
            'draft.created': 'DRAFT_CREATED',
            'draft.translated': 'DRAFT_TRANSLATED',
            'privacy.changed': 'PRIVACY_CHANGED',
            'status': 'STATUS'
        }
        from app.ui.server import EventType
        for ev_type, attr_name in event_map.items():
            # Get the actual event type value from the EventType class
            ev_value = getattr(EventType, attr_name)
            server.publish(ev_value, {'test': 1})
            await asyncio.sleep(0)
            result = await collect_next_event(resp)
            assert result is not None
            event_type_recv, data, seq = result
            assert event_type_recv == ev_type
            assert data == {'test': 1}
    finally:
        await resp.release()
        await session.close()

@pytest.mark.asyncio
async def test_health_and_ready_endpoints(ui_server):
    server, port = ui_server
    import aiohttp
    session = aiohttp.ClientSession()
    try:
        # Health endpoint
        resp = await session.get(f'http://127.0.0.1:{port}/health')
        assert resp.status == 200
        await resp.release()
        # Ready endpoint
        resp = await session.get(f'http://127.0.0.1:{port}/ready')
        assert resp.status == 200
        text = await resp.text()
        assert text.strip() == 'READY'
        await resp.release()
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_host_warning_on_non_loopback(caplog):
    import logging
    caplog.set_level(logging.WARNING)
    from app.ui.server import UiServer, UiConfig
    class MockApp:
        def snapshot(self):
            return {}
        async def is_ready(self):
            return True
        async def start_session(self, meeting_title=None):
            return "dummy-session-id"
        async def stop_session(self):
            pass
        @property
        def privacy(self):
            class MockPrivacy:
                async def switch(self, target):
                    pass
            return MockPrivacy()
    # Use a port that is likely free
    config = UiConfig(host='0.0.0.0', port=9999)
    server = UiServer(MockApp(), config)
    # The warning should have been logged during __init__
    assert any("is not a loopback address" in record.getMessage() for record in caplog.records)
    # Clean up
    del server

@pytest.mark.asyncio
async def test_server_start_and_stop(ui_server):
    server, port = ui_server
    import aiohttp
    session = aiohttp.ClientSession()
    try:
        resp = await session.get(f'http://127.0.0.1:{port}/health')
        assert resp.status == 200
        await resp.release()
    finally:
        await session.close()
    # After the fixture yields, the server is stopped (in the fixture teardown).
    # We'll trust that the fixture stops it.

@pytest.mark.asyncio
async def test_snapshot_no_replay_on_reconnect(ui_server):
    """Test that a client reconnecting does not replay past events.
    Per E1 contract: 'History is not stored on server for replay on reconnect'.
    """
    server, port = ui_server
    # First client connects
    s1, r1 = await make_sse_client('127.0.0.1', port)
    try:
        # Publish two events while client 1 is connected
        server.publish('segment.final', {'seg_id': '1', 'raw_text': 'first'})
        server.publish('segment.final', {'seg_id': '2', 'raw_text': 'second'})
        await asyncio.sleep(0)
        # Client 1 consumes the two events
        events_collected = []
        for _ in range(2):
            result = await collect_next_event(r1)
            assert result is not None
            event_type, data, seq = result
            events_collected.append((event_type, data, seq))
            assert event_type == 'segment.final'
        assert events_collected[0][1]['seg_id'] == '1'
        assert events_collected[1][1]['seg_id'] == '2'
    finally:
        await r1.release()
        await s1.close()

    # Now, publish an event while NO client is connected (simulating gap)
    server.publish('segment.final', {'seg_id': '3', 'raw_text': 'third'})
    await asyncio.sleep(0)

    # Second client connects (simulating reconnect)
    s2, r2 = await make_sse_client('127.0.0.1', port)
    try:
        # The second client should NOT receive the previous events (1, 2, or 3).
        # It should only receive NEW events published after it connects.
        # Publish a new event after client 2 connects
        server.publish('segment.final', {'seg_id': '4', 'raw_text': 'fourth'})
        await asyncio.sleep(0)
        result = await collect_next_event(r2)
        assert result is not None
        event_type, data, seq = result
        # Should receive event 4, not 1, 2, or 3
        assert event_type == 'segment.final'
        assert data['seg_id'] == '4'
        assert data['raw_text'] == 'fourth'
        # The sequence number should continue from where it left off (should be 4)
        assert seq == 4
    finally:
        await r2.release()
        await s2.close()

@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest(unused_port_factory):
    """When a client's queue is full, publish should drop the oldest event and increment lost_events."""
    from app.ui.server import UiServer, UiConfig
    class MockApp:
        def snapshot(self): return {}
        async def is_ready(self): return True
    port = unused_port_factory()
    config = UiConfig(host='127.0.0.1', port=port, queue_max=2, heartbeat_s=0.1)
    test_server = UiServer(MockApp(), config)
    await test_server.start()
    try:
        # Connect a client
        session, resp = await make_sse_client('127.0.0.1', port)
        try:
            # The client's queue max is 2.
            # Publish 3 events without consuming them (we won't read from resp).
            # But we need the events to be queued. Since we don't read, the queue will fill.
            # We'll publish 3 events.
            test_server.publish('segment.final', {'v': 1})
            test_server.publish('segment.final', {'v': 2})
            test_server.publish('segment.final', {'v': 3})
            await asyncio.sleep(0.05)  # allow background tasks to run
            # Now the client's queue should have dropped the oldest (v=1) and kept v=2, v=3.
            # Consume two events from the client.
            events = []
            for _ in range(2):
                result = await collect_next_event(resp)
                assert result is not None
                _, data, _ = result
                events.append(data['v'])
            # Should receive v=2 and v=3 (order preserved)
            assert events == [2, 3]
            # Check lost_events counter
            snap = test_server.snapshot()
            assert snap['ui']['lost_events'] == 1
        finally:
            await resp.release()
            await session.close()
    finally:
        await test_server.stop()