"""NetEase Cloud Music Listen Together tools for the ChatGPT MCP wrapper."""

import re
import threading
import time
import urllib.parse


_SESSION_LOCK = threading.Lock()
_SESSIONS = {}


def _api_ok(result):
    return isinstance(result, dict) and result.get("code", 200) == 200


def _extract_share_params(share_url):
    if not share_url:
        return {}
    match = re.search(r"https?://[^\s]+", str(share_url))
    value = match.group(0) if match else str(share_url).strip()
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    return {key: values[0] for key, values in query.items() if values}


def _current_user_id(core):
    result = core.netease_request('/api/w/nuser/account/get', method='GET')
    if not _api_ok(result):
        return None, result
    uid = result.get('account', {}).get('id')
    if not uid:
        return None, result
    return uid, result


def _room_id_from_create(result):
    data = result.get('data', {}) if isinstance(result, dict) else {}
    room_info = data.get('roomInfo', {}) if isinstance(data, dict) else {}
    return room_info.get('roomId') or data.get('roomId') or result.get('roomId')


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_play_status(value):
    value = str(value or 'PLAY').upper()
    return value if value in ('PLAY', 'PAUSE') else 'PLAY'


def _heartbeat(core, room_id, song_id, play_status='PLAY', progress_ms=0):
    return core.netease_request(
        '/api/listen/together/heartbeat',
        {
            'roomId': str(room_id),
            'songId': str(song_id),
            'playStatus': _normalize_play_status(play_status),
            'progress': max(0, _as_int(progress_ms)),
        },
    )


def _remote_status(core):
    return core.netease_request('/api/listen/together/status/get', method='POST')


def _session_snapshot(state):
    if not state:
        return None
    now_mono = time.monotonic()
    progress = state['base_progress_ms']
    if state['play_status'] == 'PLAY':
        progress += int(max(0.0, now_mono - state['base_monotonic']) * 1000)
    return {
        'active': not state['stop_event'].is_set(),
        'room_id': state['room_id'],
        'song_id': state['song_id'],
        'play_status': state['play_status'],
        'progress_ms': max(0, progress),
        'interval_seconds': state['interval_seconds'],
        'started_at_unix': state['started_at_unix'],
        'heartbeat_count': state['heartbeat_count'],
        'last_heartbeat_at_unix': state['last_heartbeat_at_unix'],
        'last_heartbeat_ok': state['last_heartbeat_ok'],
        'last_heartbeat_result': state['last_heartbeat_result'],
        'last_remote_status': state['last_remote_status'],
    }


def _get_session(room_id):
    with _SESSION_LOCK:
        return _SESSIONS.get(str(room_id))


def _stop_session(room_id):
    room_id = str(room_id)
    with _SESSION_LOCK:
        state = _SESSIONS.pop(room_id, None)
    if state:
        state['stop_event'].set()
        return _session_snapshot(state)
    return None


def _start_session(core, room_id, song_id, play_status='PLAY', progress_ms=0, interval_seconds=8):
    room_id = str(room_id)
    song_id = str(song_id)
    play_status = _normalize_play_status(play_status)
    progress_ms = max(0, _as_int(progress_ms))
    try:
        interval_seconds = float(interval_seconds)
    except (TypeError, ValueError):
        interval_seconds = 8.0
    interval_seconds = min(max(interval_seconds, 3.0), 60.0)

    _stop_session(room_id)

    first_heartbeat = _heartbeat(core, room_id, song_id, play_status, progress_ms)
    first_status = _remote_status(core)
    stop_event = threading.Event()
    state = {
        'room_id': room_id,
        'song_id': song_id,
        'play_status': play_status,
        'base_progress_ms': progress_ms,
        'base_monotonic': time.monotonic(),
        'interval_seconds': interval_seconds,
        'started_at_unix': int(time.time()),
        'heartbeat_count': 1,
        'last_heartbeat_at_unix': int(time.time()),
        'last_heartbeat_ok': _api_ok(first_heartbeat),
        'last_heartbeat_result': first_heartbeat,
        'last_remote_status': first_status,
        'stop_event': stop_event,
        'thread': None,
    }

    with _SESSION_LOCK:
        _SESSIONS[room_id] = state

    def worker():
        while not stop_event.wait(interval_seconds):
            with _SESSION_LOCK:
                current = _SESSIONS.get(room_id)
                if current is not state:
                    return
                song = state['song_id']
                status = state['play_status']
                progress = state['base_progress_ms']
                if status == 'PLAY':
                    progress += int(max(0.0, time.monotonic() - state['base_monotonic']) * 1000)

            heartbeat_result = _heartbeat(core, room_id, song, status, progress)
            remote_status = _remote_status(core)
            now = int(time.time())
            with _SESSION_LOCK:
                current = _SESSIONS.get(room_id)
                if current is not state:
                    return
                state['heartbeat_count'] += 1
                state['last_heartbeat_at_unix'] = now
                state['last_heartbeat_ok'] = _api_ok(heartbeat_result)
                state['last_heartbeat_result'] = heartbeat_result
                state['last_remote_status'] = remote_status

    thread = threading.Thread(
        target=worker,
        name=f'netease-listen-together-{room_id[:12]}',
        daemon=True,
    )
    state['thread'] = thread
    thread.start()
    return _session_snapshot(state)


def _update_session(room_id, song_id=None, play_status=None, progress_ms=None):
    room_id = str(room_id)
    with _SESSION_LOCK:
        state = _SESSIONS.get(room_id)
        if not state:
            return None

        now_mono = time.monotonic()
        current_progress = state['base_progress_ms']
        if state['play_status'] == 'PLAY':
            current_progress += int(max(0.0, now_mono - state['base_monotonic']) * 1000)

        if song_id not in (None, ''):
            state['song_id'] = str(song_id)
        if progress_ms not in (None, ''):
            current_progress = max(0, _as_int(progress_ms))
        if play_status not in (None, ''):
            state['play_status'] = _normalize_play_status(play_status)

        state['base_progress_ms'] = current_progress
        state['base_monotonic'] = now_mono
        return _session_snapshot(state)


def _resolve_room_and_song(params):
    share = _extract_share_params(params.get('share_url'))
    room_id = params.get('room_id') or share.get('roomId')
    song_id = params.get('song_id') or share.get('songId')
    return share, room_id, song_id


def _make_handlers(core):
    def listen_together_create(params):
        """Create a Listen Together room and return a share link."""
        uid, account_result = _current_user_id(core)
        if not uid:
            return {"error": "Cannot determine the logged-in NetEase user", "detail": account_result}

        result = core.netease_request(
            '/api/listen/together/room/create',
            {'refer': 'songplay_more'},
        )
        if not _api_ok(result):
            return {"error": "Failed to create Listen Together room", "detail": result}

        room_id = _room_id_from_create(result)
        if not room_id:
            return {"error": "Room was created but roomId was not returned", "detail": result}

        song_id = params.get('song_id')
        query = {'roomId': str(room_id), 'inviterId': str(uid)}
        if song_id not in (None, ''):
            query['songId'] = str(song_id)
        share_url = 'https://st.music.163.com/listen-together/share/index.html?' + urllib.parse.urlencode(query)
        return {
            "status": "created",
            "room_id": str(room_id),
            "inviter_id": str(uid),
            "song_id": str(song_id) if song_id not in (None, '') else None,
            "share_url": share_url,
            "room_info": result.get('data', {}).get('roomInfo'),
        }

    def listen_together_join(params):
        """Accept an invitation and optionally start persistent heartbeats."""
        share, room_id, song_id = _resolve_room_and_song(params)
        inviter_id = params.get('inviter_id') or share.get('inviterId')
        if not room_id or not inviter_id:
            return {"error": "share_url or both room_id and inviter_id are required"}

        accepted = core.netease_request(
            '/api/listen/together/play/invitation/accept',
            {'refer': 'inbox_invite', 'roomId': room_id, 'inviterId': inviter_id},
        )
        if not _api_ok(accepted):
            return {"error": "Failed to accept Listen Together invitation", "detail": accepted}

        checked = core.netease_request('/api/listen/together/room/check', {'roomId': room_id})
        playlist = core.netease_request('/api/listen/together/sync/playlist/get', {'roomId': room_id})
        remote_status = _remote_status(core)

        auto_heartbeat = params.get('auto_heartbeat', True)
        heartbeat_session = None
        heartbeat_note = None
        if auto_heartbeat:
            if song_id:
                heartbeat_session = _start_session(
                    core,
                    room_id,
                    song_id,
                    params.get('play_status', 'PLAY'),
                    params.get('progress_ms', 0),
                    params.get('interval_seconds', 8),
                )
            else:
                heartbeat_note = (
                    'Joined, but persistent heartbeat was not started because no song_id '
                    'was supplied and the share URL did not contain songId.'
                )

        return {
            "status": "joined",
            "room_id": str(room_id),
            "inviter_id": str(inviter_id),
            "song_id": str(song_id) if song_id else None,
            "room": checked,
            "playlist": playlist,
            "remote_status": remote_status,
            "heartbeat_session": heartbeat_session,
            "note": heartbeat_note,
        }

    def listen_together_keepalive(params):
        """Start or restart the persistent heartbeat worker for a room."""
        _, room_id, song_id = _resolve_room_and_song(params)
        if not room_id:
            return {"error": "room_id or share_url is required"}
        if not song_id:
            return {"error": "song_id is required, or provide a share_url containing songId"}
        session = _start_session(
            core,
            room_id,
            song_id,
            params.get('play_status', 'PLAY'),
            params.get('progress_ms', 0),
            params.get('interval_seconds', 8),
        )
        return {"status": "keepalive_started", "session": session}

    def listen_together_heartbeat(params):
        """Send one heartbeat and update a running local session if present."""
        _, room_id, song_id = _resolve_room_and_song(params)
        if not room_id:
            return {"error": "room_id or share_url is required"}
        running = _get_session(room_id)
        if not song_id and running:
            song_id = running['song_id']
        if not song_id:
            return {"error": "song_id is required, or provide a share_url containing songId"}

        play_status = params.get('play_status')
        progress_ms = params.get('progress_ms')
        if running:
            updated = _update_session(room_id, song_id, play_status, progress_ms)
            play_status = updated['play_status']
            progress_ms = updated['progress_ms']
        else:
            play_status = _normalize_play_status(play_status or 'PLAY')
            progress_ms = max(0, _as_int(progress_ms))

        result = _heartbeat(core, room_id, song_id, play_status, progress_ms)
        return {
            "status": "heartbeat_sent" if _api_ok(result) else "heartbeat_failed",
            "room_id": str(room_id),
            "song_id": str(song_id),
            "play_status": play_status,
            "progress_ms": progress_ms,
            "detail": result,
            "session": _session_snapshot(_get_session(room_id)),
        }

    def listen_together_presence(params):
        """Inspect the local heartbeat worker and NetEase in-room/member state."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        local = _session_snapshot(_get_session(room_id)) if room_id else None
        remote = _remote_status(core)
        checked = None
        if room_id:
            checked = core.netease_request('/api/listen/together/room/check', {'roomId': room_id})
        return {
            "room_id": str(room_id) if room_id else None,
            "local_session": local,
            "remote_status": remote,
            "room_check": checked,
        }

    def listen_together_stop_keepalive(params):
        """Stop local heartbeat maintenance without ending the NetEase room."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        if not room_id:
            return {"error": "room_id or share_url is required"}
        stopped = _stop_session(room_id)
        return {
            "status": "keepalive_stopped" if stopped else "no_keepalive_running",
            "room_id": str(room_id),
            "session": stopped,
        }

    def listen_together_room(params):
        """Get current room information, synchronized playlist and member state."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        if not room_id:
            return {"error": "room_id or share_url is required"}

        checked = core.netease_request('/api/listen/together/room/check', {'roomId': room_id})
        if not _api_ok(checked):
            return {"error": "Failed to get Listen Together room", "detail": checked}
        playlist = core.netease_request('/api/listen/together/sync/playlist/get', {'roomId': room_id})
        remote_status = _remote_status(core)
        return {
            "room_id": str(room_id),
            "room": checked,
            "playlist": playlist,
            "remote_status": remote_status,
            "heartbeat_session": _session_snapshot(_get_session(room_id)),
        }

    def listen_together_leave(params):
        """Stop heartbeats and leave/end a Listen Together room."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        if not room_id:
            return {"error": "room_id or share_url is required"}

        stopped = _stop_session(room_id)
        result = core.netease_request('/api/listen/together/end/v2', {'roomId': room_id})
        if not _api_ok(result):
            return {"error": "Failed to leave Listen Together room", "detail": result, "heartbeat_session": stopped}
        return {"status": "left", "room_id": str(room_id), "detail": result, "heartbeat_session": stopped}

    return {
        'listen_together_create': listen_together_create,
        'listen_together_join': listen_together_join,
        'listen_together_keepalive': listen_together_keepalive,
        'listen_together_heartbeat': listen_together_heartbeat,
        'listen_together_presence': listen_together_presence,
        'listen_together_stop_keepalive': listen_together_stop_keepalive,
        'listen_together_room': listen_together_room,
        'listen_together_leave': listen_together_leave,
    }


_ROOM_PROPS = {
    "share_url": {"type": "string", "description": "Official st.music.163.com Listen Together share URL."},
    "room_id": {"type": "string", "description": "Listen Together room ID."},
}

_HEARTBEAT_PROPS = {
    **_ROOM_PROPS,
    "song_id": {"type": "integer", "description": "Current song ID; optional when share_url contains songId."},
    "play_status": {"type": "string", "enum": ["PLAY", "PAUSE"], "description": "Playback state; default PLAY."},
    "progress_ms": {"type": "integer", "description": "Playback progress in milliseconds; default 0."},
}

TOOLS = [
    {
        "name": "listen_together_create",
        "description": "Create a NetEase Cloud Music Listen Together room and return an official share URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer", "description": "Optional song ID to include in the share URL."}
            },
        },
    },
    {
        "name": "listen_together_join",
        "description": "Join a Listen Together room and, by default, maintain presence with background heartbeats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_HEARTBEAT_PROPS,
                "inviter_id": {"type": "string", "description": "Inviter user ID when not using share_url."},
                "auto_heartbeat": {"type": "boolean", "description": "Start background heartbeats after joining; default true."},
                "interval_seconds": {"type": "number", "description": "Heartbeat interval, clamped to 3-60 seconds; default 8."},
            },
        },
    },
    {
        "name": "listen_together_keepalive",
        "description": "Start or restart background heartbeats to keep this account present in a Listen Together room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_HEARTBEAT_PROPS,
                "interval_seconds": {"type": "number", "description": "Heartbeat interval, clamped to 3-60 seconds; default 8."},
            },
        },
    },
    {
        "name": "listen_together_heartbeat",
        "description": "Send one Listen Together heartbeat with song, playback state and millisecond progress.",
        "inputSchema": {"type": "object", "properties": _HEARTBEAT_PROPS},
    },
    {
        "name": "listen_together_presence",
        "description": "Check local heartbeat maintenance plus NetEase in-room status and room members.",
        "inputSchema": {"type": "object", "properties": _ROOM_PROPS},
    },
    {
        "name": "listen_together_stop_keepalive",
        "description": "Stop background heartbeats without ending the Listen Together room.",
        "inputSchema": {"type": "object", "properties": _ROOM_PROPS},
    },
    {
        "name": "listen_together_room",
        "description": "Get Listen Together room information, synchronized playlist, member status, and heartbeat state.",
        "inputSchema": {"type": "object", "properties": _ROOM_PROPS},
    },
    {
        "name": "listen_together_leave",
        "description": "Stop local heartbeats and leave/end a NetEase Cloud Music Listen Together room.",
        "inputSchema": {"type": "object", "properties": _ROOM_PROPS},
    },
]


def register(core):
    """Register Listen Together tools on the existing MCP core module."""
    existing = {tool.get('name') for tool in core.TOOLS}
    core.TOOLS.extend(tool for tool in TOOLS if tool['name'] not in existing)
    core.TOOL_DISPATCH.update(_make_handlers(core))
