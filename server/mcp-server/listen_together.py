"""NetEase Cloud Music Listen Together tools for the ChatGPT MCP wrapper."""

import re
import urllib.parse


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
        """Accept a Listen Together invitation from a share URL or IDs."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
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
        return {
            "status": "joined",
            "room_id": str(room_id),
            "inviter_id": str(inviter_id),
            "room": checked,
            "playlist": playlist,
        }

    def listen_together_room(params):
        """Get current room information and synchronized playlist."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        if not room_id:
            return {"error": "room_id or share_url is required"}

        checked = core.netease_request('/api/listen/together/room/check', {'roomId': room_id})
        if not _api_ok(checked):
            return {"error": "Failed to get Listen Together room", "detail": checked}
        playlist = core.netease_request('/api/listen/together/sync/playlist/get', {'roomId': room_id})
        return {"room_id": str(room_id), "room": checked, "playlist": playlist}

    def listen_together_leave(params):
        """Leave/end a Listen Together room."""
        share = _extract_share_params(params.get('share_url'))
        room_id = params.get('room_id') or share.get('roomId')
        if not room_id:
            return {"error": "room_id or share_url is required"}

        result = core.netease_request('/api/listen/together/end/v2', {'roomId': room_id})
        if not _api_ok(result):
            return {"error": "Failed to leave Listen Together room", "detail": result}
        return {"status": "left", "room_id": str(room_id), "detail": result}

    return {
        'listen_together_create': listen_together_create,
        'listen_together_join': listen_together_join,
        'listen_together_room': listen_together_room,
        'listen_together_leave': listen_together_leave,
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
        "description": "Join a NetEase Cloud Music Listen Together room using an official share URL, or room_id plus inviter_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_url": {"type": "string", "description": "Official st.music.163.com Listen Together share URL."},
                "room_id": {"type": "string", "description": "Room ID when not using share_url."},
                "inviter_id": {"type": "string", "description": "Inviter user ID when not using share_url."}
            },
        },
    },
    {
        "name": "listen_together_room",
        "description": "Get Listen Together room information and the synchronized playlist.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_url": {"type": "string", "description": "Official Listen Together share URL."},
                "room_id": {"type": "string", "description": "Listen Together room ID."}
            },
        },
    },
    {
        "name": "listen_together_leave",
        "description": "Leave/end a NetEase Cloud Music Listen Together room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_url": {"type": "string", "description": "Official Listen Together share URL."},
                "room_id": {"type": "string", "description": "Listen Together room ID."}
            },
        },
    },
]


def register(core):
    """Register Listen Together tools on the existing MCP core module."""
    existing = {tool.get('name') for tool in core.TOOLS}
    core.TOOLS.extend(tool for tool in TOOLS if tool['name'] not in existing)
    core.TOOL_DISPATCH.update(_make_handlers(core))
