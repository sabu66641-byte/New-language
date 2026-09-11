"""
ps_discord.py
ps向け Discord Bot / User ランタイム（単一ファイル版）

依存:
    pip install websockets

このファイルは Discord API / Gateway を扱うランタイム。
現在の app.py の Lexer / Parser / C++ Generator とは独立している。

主な機能:
- Gateway v10
- Identify / Heartbeat / Resume / Reconnect
- イベント登録
- メッセージ送信・編集・削除・取得・リアクション
- Guild / Channel / Member / Role API
- Webhook API
- Slash Command の登録・削除
- Interaction イベント
- REST の簡易 rate-limit 待機
- sync API と async API
- Botトークンおよびユーザートークン（Selfbot）の両対応
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE = "https://discord.com/api/v10"
GATEWAY_BASE = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordError(Exception):
    """Discord API / Gateway 関連エラー。"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        data: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.data = data


class DiscordClient:
    """
    ps用 Discord Bot / User クライアント。

    例:
        bot = DiscordClient("BOT_TOKEN_OR_USER_TOKEN")

        @bot.event("ready")
        def ready(data):
            print("Bot ready:", data.get("user", {}).get("username"))

        @bot.event("message")
        def message(data):
            if data.get("content") == "!ping":
                bot.send_message(data["channel_id"], "Pong!")

        bot.start()
    """

    # Gateway intents
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    GUILD_MODERATION = 1 << 2
    GUILD_EMOJIS_AND_STICKERS = 1 << 3
    GUILD_INTEGRATIONS = 1 << 4
    GUILD_WEBHOOKS = 1 << 5
    GUILD_INVITES = 1 << 6
    GUILD_VOICE_STATES = 1 << 7
    GUILD_PRESENCES = 1 << 8
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    GUILD_MESSAGE_TYPING = 1 << 11
    DIRECT_MESSAGES = 1 << 12
    DIRECT_MESSAGE_REACTIONS = 1 << 13
    DIRECT_MESSAGE_TYPING = 1 << 14
    MESSAGE_CONTENT = 1 << 15
    GUILD_SCHEDULED_EVENTS = 1 << 16
    AUTO_MODERATION_CONFIGURATION = 1 << 20
    AUTO_MODERATION_EXECUTION = 1 << 21
    GUILD_MESSAGE_POLLS = 1 << 24
    DIRECT_MESSAGE_POLLS = 1 << 25

    def __init__(
        self,
        token: str,
        *,
        intents: Optional[int] = None,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        reconnect_delay: float = 5.0,
        is_user: Optional[bool] = None,
    ):
        if not token or not isinstance(token, str):
            raise ValueError("Discord Bot token が必要です。")

        cleaned_token = token.strip()
        if cleaned_token.startswith("Bot "):
            cleaned_token = cleaned_token[4:].strip()

        self.token = cleaned_token
        
        # ユーザートークン自動判別 (明示指定がない場合)
        if is_user is not None:
            self.is_user = is_user
        else:
            # ユーザートークンはドット区切り2つ（ベース64構成）、または特定のプレフィックスを持たないケースに対応
            parts = self.token.split(".")
            self.is_user = len(parts) == 3 and not self.token.startswith("MTI") if len(parts) == 3 else False

        self.intents = (
            intents
            if intents is not None
            else self.GUILDS
            | self.GUILD_MESSAGES
            | self.MESSAGE_CONTENT
        )
        self.user_agent = user_agent
        self.reconnect_delay = reconnect_delay

        self._events: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False

        # Gateway session state
        self.session_id: Optional[str] = None
        self.resume_gateway_url: Optional[str] = None
        self.sequence: Optional[int] = None
        self.heartbeat_interval: Optional[float] = None
        self.user: Optional[dict[str, Any]] = None
        self.application: Optional[dict[str, Any]] = None

        # REST rate-limit state
        self._rate_lock = threading.Lock()
        self._bucket_state: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

    def on(self, event_name: str, callback: Callable[..., Any]):
        """イベント登録。"""
        self._events[event_name.lower()].append(callback)
        return callback

    def event(self, event_name: str):
        """デコレータ形式のイベント登録。"""
        def decorator(callback):
            self.on(event_name, callback)
            return callback
        return decorator

    def off(self, event_name: str, callback: Callable[..., Any]):
        """イベント解除。"""
        name = event_name.lower()
        if callback in self._events.get(name, []):
            self._events[name].remove(callback)

    async def _emit(self, event_name: str, *args):
        callbacks = list(self._events.get(event_name.lower(), []))
        for callback in callbacks:
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                # Bot本体をイベント一つの例外で落とさない
                print(f"[ps-discord] event '{event_name}' error: {exc}")

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        auth_header = self.token if self.is_user else f"Bot {self.token}"
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        data: Any = None,
        query: Optional[dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Any:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        url = API_BASE + endpoint
        if query:
            parts = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    value = str(value).lower()
                parts.append(f"{quote(str(key))}={quote(str(value))}")
            if parts:
                url += "?" + "&".join(parts)

        body = None
        json_body = data is not None

        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        # 少数回の再試行。429 と一時的な通信失敗を対象にする。
        for attempt in range(4):
            req = Request(
                url,
                data=body,
                headers=self._headers(json_body=json_body),
                method=method.upper(),
            )

            try:
                with urlopen(req, timeout=timeout) as response:
                    raw = response.read()
                    return self._decode_response(raw)

            except HTTPError as exc:
                raw = exc.read()
                payload = self._decode_response(raw)

                if exc.code == 429 and attempt < 3:
                    retry_after = self._extract_retry_after(payload)
                    time.sleep(max(0.1, retry_after))
                    continue

                message = self._discord_error_message(payload, exc.code)
                raise DiscordError(
                    message,
                    status=exc.code,
                    code=self._extract_error_code(payload),
                    data=payload,
                ) from exc

            except URLError as exc:
                if attempt < 3:
                    time.sleep(1.0 + attempt)
                    continue
                raise DiscordError(f"通信エラー: {exc}") from exc

        raise DiscordError("Discord APIへの接続に失敗しました。")

    @staticmethod
    def _decode_response(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _extract_retry_after(payload: Any) -> float:
        if isinstance(payload, dict):
            value = payload.get("retry_after")
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        return 1.0

    @staticmethod
    def _extract_error_code(payload: Any) -> Optional[int]:
        if isinstance(payload, dict):
            code = payload.get("code")
            return code if isinstance(code, int) else None
        return None

    @staticmethod
    def _discord_error_message(payload: Any, status: int) -> str:
        if isinstance(payload, dict):
            message = payload.get("message")
            if message:
                return f"Discord API error ({status}): {message}"
        return f"Discord API error ({status})"

    # Generic REST helpers
    def get(self, endpoint: str, **kwargs):
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, data: Any = None, **kwargs):
        return self._request("POST", endpoint, data=data, **kwargs)

    def put(self, endpoint: str, data: Any = None, **kwargs):
        return self._request("PUT", endpoint, data=data, **kwargs)

    def patch(self, endpoint: str, data: Any = None, **kwargs):
        return self._request("PATCH", endpoint, data=data, **kwargs)

    def delete(self, endpoint: str, **kwargs):
        return self._request("DELETE", endpoint, **kwargs)

    # ------------------------------------------------------------------
    # Bot / user
    # ------------------------------------------------------------------

    def get_me(self):
        return self.get("/users/@me")

    def get_application(self):
        if self.application is not None:
            return self.application
        if self.is_user:
            return {}
        self.application = self.get("/oauth2/applications/@me")
        return self.application

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def send_message(
        self,
        channel_id: str,
        content: Optional[str] = None,
        *,
        embeds: Optional[list[dict[str, Any]]] = None,
        components: Optional[list[dict[str, Any]]] = None,
        allowed_mentions: Optional[dict[str, Any]] = None,
        tts: bool = False,
    ):
        data: dict[str, Any] = {"tts": tts}
        if content is not None:
            data["content"] = content
        if embeds is not None:
            data["embeds"] = embeds
        if components is not None:
            data["components"] = components
        if allowed_mentions is not None:
            data["allowed_mentions"] = allowed_mentions

        return self.post(f"/channels/{channel_id}/messages", data)

    def get_message(self, channel_id: str, message_id: str):
        return self.get(f"/channels/{channel_id}/messages/{message_id}")

    def get_messages(self, channel_id: str, *, limit: int = 50, before=None, after=None, around=None):
        limit = max(1, min(100, int(limit)))
        return self.get(
            f"/channels/{channel_id}/messages",
            query={"limit": limit, "before": before, "after": after, "around": around},
        )

    def edit_message(self, channel_id: str, message_id: str, **fields):
        allowed = {
            "content",
            "embeds",
            "components",
            "allowed_mentions",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        return self.patch(f"/channels/{channel_id}/messages/{message_id}", data)

    def delete_message(self, channel_id: str, message_id: str):
        return self.delete(f"/channels/{channel_id}/messages/{message_id}")

    def add_reaction(self, channel_id: str, message_id: str, emoji: str):
        encoded = quote(emoji, safe="")
        return self.put(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        )

    def remove_own_reaction(self, channel_id: str, message_id: str, emoji: str):
        encoded = quote(emoji, safe="")
        return self.delete(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        )

    def trigger_typing(self, channel_id: str):
        return self.post(f"/channels/{channel_id}/typing")

    # ------------------------------------------------------------------
    # Guilds / channels / members / roles
    # ------------------------------------------------------------------

    def get_guild(self, guild_id: str):
        return self.get(f"/guilds/{guild_id}")

    def get_guild_channels(self, guild_id: str):
        return self.get(f"/guilds/{guild_id}/channels")

    def create_channel(self, guild_id: str, name: str, *, channel_type: int = 0, **fields):
        data = {"name": name, "type": channel_type}
        data.update(fields)
        return self.post(f"/guilds/{guild_id}/channels", data)

    def edit_channel(self, channel_id: str, **fields):
        return self.patch(f"/channels/{channel_id}", fields)

    def delete_channel(self, channel_id: str):
        return self.delete(f"/channels/{channel_id}")

    def get_member(self, guild_id: str, user_id: str):
        return self.get(f"/guilds/{guild_id}/members/{user_id}")

    def get_guild_members(self, guild_id: str, *, limit: int = 1000, after=None):
        limit = max(1, min(1000, int(limit)))
        return self.get(
            f"/guilds/{guild_id}/members",
            query={"limit": limit, "after": after},
        )

    def add_role(self, guild_id: str, user_id: str, role_id: str):
        return self.put(f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")

    def remove_role(self, guild_id: str, user_id: str, role_id: str):
        return self.delete(f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")

    def get_roles(self, guild_id: str):
        return self.get(f"/guilds/{guild_id}/roles")

    def create_role(self, guild_id: str, name: str, **fields):
        data = {"name": name}
        data.update(fields)
        return self.post(f"/guilds/{guild_id}/roles", data)

    def edit_role(self, guild_id: str, role_id: str, **fields):
        return self.patch(f"/guilds/{guild_id}/roles/{role_id}", fields)

    def delete_role(self, guild_id: str, role_id: str):
        return self.delete(f"/guilds/{guild_id}/roles/{role_id}")

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def get_channel_webhooks(self, channel_id: str):
        return self.get(f"/channels/{channel_id}/webhooks")

    def get_webhook(self, webhook_id: str):
        return self.get(f"/webhooks/{webhook_id}")

    def create_webhook(self, channel_id: str, name: str, **fields):
        data = {"name": name}
        data.update(fields)
        return self.post(f"/channels/{channel_id}/webhooks", data)

    def edit_webhook(self, webhook_id: str, **fields):
        return self.patch(f"/webhooks/{webhook_id}", fields)

    def delete_webhook(self, webhook_id: str):
        return self.delete(f"/webhooks/{webhook_id}")

    def execute_webhook(
        self,
        webhook_id: str,
        webhook_token: str,
        *,
        content: Optional[str] = None,
        username: Optional[str] = None,
        avatar_url: Optional[str] = None,
        embeds: Optional[list[dict[str, Any]]] = None,
        components: Optional[list[dict[str, Any]]] = None,
        wait: bool = False,
    ):
        data = {}
        if content is not None:
            data["content"] = content
        if username is not None:
            data["username"] = username
        if avatar_url is not None:
            data["avatar_url"] = avatar_url
        if embeds is not None:
            data["embeds"] = embeds
        if components is not None:
            data["components"] = components

        return self._request(
            "POST",
            f"/webhooks/{webhook_id}/{webhook_token}",
            data=data,
            query={"wait": wait},
        )

    # ------------------------------------------------------------------
    # Application commands
    # ------------------------------------------------------------------

    def list_global_commands(self):
        app_id = self._application_id()
        return self.get(f"/applications/{app_id}/commands")

    def create_global_command(self, name: str, description: str, options=None, **fields):
        app_id = self._application_id()
        data = {
            "name": name,
            "description": description,
        }
        if options is not None:
            data["options"] = options
        data.update(fields)
        return self.post(f"/applications/{app_id}/commands", data)

    def edit_global_command(self, command_id: str, **fields):
        app_id = self._application_id()
        return self.patch(f"/applications/{app_id}/commands/{command_id}", fields)

    def delete_global_command(self, command_id: str):
        app_id = self._application_id()
        return self.delete(f"/applications/{app_id}/commands/{command_id}")

    def list_guild_commands(self, guild_id: str):
        app_id = self._application_id()
        return self.get(f"/applications/{app_id}/guilds/{guild_id}/commands")

    def create_guild_command(
        self,
        guild_id: str,
        name: str,
        description: str,
        options=None,
        **fields,
    ):
        app_id = self._application_id()
        data = {
            "name": name,
            "description": description,
        }
        if options is not None:
            data["options"] = options
        data.update(fields)
        return self.post(
            f"/applications/{app_id}/guilds/{guild_id}/commands",
            data,
        )

    def edit_guild_command(self, guild_id: str, command_id: str, **fields):
        app_id = self._application_id()
        return self.patch(
            f"/applications/{app_id}/guilds/{guild_id}/commands/{command_id}",
            fields,
        )

    def delete_guild_command(self, guild_id: str, command_id: str):
        app_id = self._application_id()
        return self.delete(
            f"/applications/{app_id}/guilds/{guild_id}/commands/{command_id}"
        )

    def _application_id(self) -> str:
        if self.application and self.application.get("id"):
            return str(self.application["id"])
        me = self.get_me()
        app_id = me.get("id")
        if not app_id:
            raise DiscordError("BotのApplication IDを取得できませんでした。")
        return str(app_id)

    # ------------------------------------------------------------------
    # Interaction response helpers
    # ------------------------------------------------------------------

    def respond_interaction(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        content: Optional[str] = None,
        embeds: Optional[list[dict[str, Any]]] = None,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ):
        data: dict[str, Any] = {"type": 4, "data": {}}
        if content is not None:
            data["data"]["content"] = content
        if embeds is not None:
            data["data"]["embeds"] = embeds
        if components is not None:
            data["data"]["components"] = components
        if ephemeral:
            data["data"]["flags"] = 64

        # Interaction callback endpoint は通常の Bot Authorization を
        # 必要としないため、専用リクエストを使う。
        endpoint = f"/interactions/{interaction_id}/{interaction_token}/callback"
        return self._request("POST", endpoint, data=data)

    def edit_original_interaction_response(
        self,
        application_id: str,
        interaction_token: str,
        **fields,
    ):
        return self.patch(
            f"/webhooks/{application_id}/{interaction_token}/messages/@original",
            fields,
        )

    def delete_original_interaction_response(
        self,
        application_id: str,
        interaction_token: str,
    ):
        return self.delete(
            f"/webhooks/{application_id}/{interaction_token}/messages/@original"
        )

    # ------------------------------------------------------------------
    # Gateway
    # ------------------------------------------------------------------

    async def _connect_gateway(self):
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            try:
                from websockets import connect
            except ImportError as exc:
                raise DiscordError(
                    "websockets が必要です。requirements.txt に websockets を追加してください。"
                ) from exc

        url = self.resume_gateway_url or GATEWAY_BASE

        async with connect(
            url,
            ping_interval=None,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws

            hello = await ws.recv()
            hello_data = json.loads(hello)

            if hello_data.get("op") != 10:
                raise DiscordError("GatewayからHELLOを受信できませんでした。")

            interval_ms = hello_data["d"]["heartbeat_interval"]
            self.heartbeat_interval = interval_ms / 1000.0

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                if self.session_id and self.sequence is not None:
                    await self._resume()
                else:
                    await self._identify()

                async for raw in ws:
                    if self._stop_event.is_set():
                        break

                    payload = json.loads(raw)
                    op = payload.get("op")
                    data = payload.get("d")
                    seq = payload.get("s")

                    if seq is not None:
                        self.sequence = seq

                    if op == 0:
                        await self._handle_dispatch(payload.get("t"), data)

                    elif op == 1:
                        await self._send_gateway(
                            {"op": 1, "d": self.sequence}
                        )

                    elif op == 7:
                        # Reconnect requested by Discord.
                        break

                    elif op == 9:
                        # Invalid session. If resumable, reconnect with a new
                        # Gateway URL/session; otherwise identify again.
                        resumable = bool(data)
                        if not resumable:
                            self.session_id = None
                            self.sequence = None
                            self.resume_gateway_url = None
                        break

                    elif op == 11:
                        await self._emit("heartbeat_ack")

            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

                self._ws = None

    async def _identify(self):
        if self.is_user:
            identify_payload = {
                "op": 2,
                "d": {
                    "token": self.token,
                    "capabilities": 16381,
                    "properties": {
                        "os": "Windows",
                        "browser": "Chrome",
                        "device": "",
                        "system_locale": "ja-JP",
                        "browser_user_agent": self.user_agent,
                        "browser_version": "120.0.0.0",
                        "os_version": "10",
                        "referrer": "",
                        "referring_domain": "",
                        "referrer_current": "",
                        "referring_domain_current": "",
                        "release_channel": "stable",
                        "client_build_number": 260000,
                        "client_event_source": None,
                    },
                    "presence": {
                        "status": "online",
                        "since": 0,
                        "activities": [],
                        "afk": False,
                    },
                    "compress": False,
                    "client_state": {
                        "guild_versions": {},
                        "highest_last_message_id": "0",
                        "read_state_version": 0,
                        "user_guild_settings_version": -1,
                        "user_settings_version": -1,
                        "private_channels_version": "0",
                        "api_code_version": 0,
                    },
                },
            }
        else:
            identify_payload = {
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": self.intents,
                    "properties": {
                        "os": "linux",
                        "browser": "ps",
                        "device": "ps",
                    },
                },
            }

        await self._send_gateway(identify_payload)

    async def _resume(self):
        await self._send_gateway(
            {
                "op": 6,
                "d": {
                    "token": self.token,
                    "session_id": self.session_id,
                    "seq": self.sequence,
                },
            }
        )

    async def _send_gateway(self, payload: dict[str, Any]):
        if self._ws is None:
            raise DiscordError("Gateway接続がありません。")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            await asyncio.sleep(self.heartbeat_interval or 41.25)
            if self._ws is not None:
                try:
                    await self._send_gateway(
                        {"op": 1, "d": self.sequence}
                    )
                except Exception:
                    return

    async def _handle_dispatch(self, event_name: Optional[str], data: Any):
        if not event_name:
            return

        if event_name == "READY":
            self.session_id = data.get("session_id")
            self.resume_gateway_url = data.get("resume_gateway_url")
            self.user = data.get("user")
            self.application = data.get("application")
            await self._emit("ready", data)
            return

        if event_name == "RESUMED":
            await self._emit("resumed", data)
            return

        # Discord event name -> ps-friendly short name
        mapping = {
            "MESSAGE_CREATE": "message",
            "MESSAGE_UPDATE": "message_update",
            "MESSAGE_DELETE": "message_delete",
            "MESSAGE_DELETE_BULK": "message_delete_bulk",
            "MESSAGE_REACTION_ADD": "reaction_add",
            "MESSAGE_REACTION_REMOVE": "reaction_remove",
            "MESSAGE_REACTION_REMOVE_ALL": "reaction_remove_all",
            "GUILD_CREATE": "guild_create",
            "GUILD_UPDATE": "guild_update",
            "GUILD_DELETE": "guild_delete",
            "GUILD_MEMBER_ADD": "member_join",
            "GUILD_MEMBER_REMOVE": "member_remove",
            "GUILD_MEMBER_UPDATE": "member_update",
            "GUILD_ROLE_CREATE": "role_create",
            "GUILD_ROLE_UPDATE": "role_update",
            "GUILD_ROLE_DELETE": "role_delete",
            "CHANNEL_CREATE": "channel_create",
            "CHANNEL_UPDATE": "channel_update",
            "CHANNEL_DELETE": "channel_delete",
            "INTERACTION_CREATE": "interaction",
            "TYPING_START": "typing",
            "PRESENCE_UPDATE": "presence",
            "VOICE_STATE_UPDATE": "voice_state_update",
        }

        short_name = mapping.get(event_name, event_name.lower())
        await self._emit(short_name, data)

        # Discordの元イベント名でも購読可能
        await self._emit(event_name.lower(), data)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_async(self):
        if self._started:
            return

        self._started = True
        self._stop_event.clear()
        self._loop = asyncio.get_running_loop()

        try:
            await self._emit("starting")

            while not self._stop_event.is_set():
                try:
                    await self._connect_gateway()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._emit("error", exc)

                if self._stop_event.is_set():
                    break

                await asyncio.sleep(self.reconnect_delay)

        finally:
            self._started = False
            await self._emit("stopped")

    def start(self, *, background: bool = True):
        """
        Bot起動。
        background=Trueなら別スレッドでGatewayを常駐させる。
        """
        if self._started:
            return

        if not background:
            asyncio.run(self.start_async())
            return

        if self._thread and self._thread.is_alive():
            return

        def runner():
            try:
                asyncio.run(self.start_async())
            except Exception as exc:
                print(f"[ps-discord] fatal: {exc}")

        self._thread = threading.Thread(
            target=runner,
            name="ps-discord-gateway",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Bot停止要求。"""
        self._stop_event.set()

        if self._loop and self._loop.is_running():
            async def close():
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass

            asyncio.run_coroutine_threadsafe(close(), self._loop)

        self._started = False

    def is_running(self) -> bool:
        return self._started

    def status(self) -> dict[str, Any]:
        return {
            "running": self._started,
            "connected": self._ws is not None,
            "session_id": self.session_id,
            "user": self.user,
            "application": self.application,
        }


# ----------------------------------------------------------------------
# ps側で使いやすくするための別名
# ----------------------------------------------------------------------

DiscordBot = DiscordClient


# ----------------------------------------------------------------------
# 単体テスト用
# ----------------------------------------------------------------------

if __name__ == "__main__":
    print("ps_discord.py loaded successfully.")
    print("DiscordBot / DiscordClient available.")
    print("Bot tokenをこのファイルに直接書くのではなく、呼び出し側から渡してください。")
