import asyncio
import json
import threading
import time

from network import get, post, json_headers


# ====================================================
# ps Discord Runtime
# Discord Bot API / Gateway
# ====================================================

DISCORD_API = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordError(Exception):
    pass


class DiscordBot:
    def __init__(self, token):
        if not token:
            raise DiscordError("Discord Bot Tokenが設定されていません。")

        self.token = token
        self.user = None
        self.running = False

        self._events = {}
        self._ws = None
        self._heartbeat_task = None
        self._gateway_task = None

    # =================================================
    # イベント登録
    # =================================================

    def on(self, event_name, callback):
        """
        Discordイベントを登録する
        """

        if event_name not in self._events:
            self._events[event_name] = []

        self._events[event_name].append(callback)

    async def emit(self, event_name, *args):
        """
        登録されたイベントを実行する
        """

        callbacks = self._events.get(event_name, [])

        for callback in callbacks:
            try:
                result = callback(*args)

                if asyncio.iscoroutine(result):
                    await result

            except Exception as e:
                print(
                    f"[Discord Event Error] "
                    f"{event_name}: {e}"
                )

    # =================================================
    # HTTP API
    # =================================================

    def _headers(self):
        return {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ps-runtime/1.0"
        }

    def api_get(self, endpoint):
        return get(
            DISCORD_API + endpoint,
            headers=self._headers()
        )

    def api_post(self, endpoint, data=None):
        return post(
            DISCORD_API + endpoint,
            data=data,
            headers=self._headers()
        )

    # =================================================
    # メッセージ送信
    # =================================================

    def send_message(self, channel_id, content):
        """
        Discordチャンネルへメッセージを送信
        """

        return self.api_post(
            f"/channels/{channel_id}/messages",
            {
                "content": str(content)
            }
        )

    # =================================================
    # Bot情報
    # =================================================

    def get_me(self):
        """
        Bot自身の情報を取得
        """

        result = self.api_get("/users/@me")

        if result["status"] >= 400:
            raise DiscordError(
                f"Discord API Error: {result['status']}"
            )

        self.user = result["data"]

        return self.user

    # =================================================
    # Gateway
    # =================================================

    async def _gateway(self):
        """
        Discord Gatewayへの接続本体
        """

        try:
            import websockets
        except ImportError:
            raise DiscordError(
                "websockets がインストールされていません。"
            )

        async with websockets.connect(
            GATEWAY_URL,
            max_size=8 * 1024 * 1024
        ) as ws:

            self._ws = ws

            # Discordから最初にHELLOが来る
            hello = json.loads(
                await ws.recv()
            )

            if hello.get("op") != 10:
                raise DiscordError(
                    "GatewayからHELLOを受信できませんでした。"
                )

            heartbeat_interval = (
                hello["d"]["heartbeat_interval"]
            ) / 1000

            # 認証
            await ws.send(
                json.dumps({
                    "op": 2,
                    "d": {
                        "token": self.token,
                        "intents": (
                            1 << 0
                            | 1 << 9
                            | 1 << 15
                        ),
                        "properties": {
                            "os": "linux",
                            "browser": "ps",
                            "device": "ps"
                        }
                    }
                })
            )

            # Heartbeat開始
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat(
                    ws,
                    heartbeat_interval
                )
            )

            # Gatewayイベント受信
            while self.running:

                raw = await ws.recv()

                packet = json.loads(raw)

                op = packet.get("op")
                event = packet.get("t")
                data = packet.get("d")

                # Heartbeat要求
                if op == 1:
                    await ws.send(
                        json.dumps({
                            "op": 1,
                            "d": None
                        })
                    )

                # Hello / Heartbeat
                elif op == 10:
                    pass

                # Dispatchイベント
                elif op == 0:
                    await self._handle_event(
                        event,
                        data
                    )

                # Reconnect
                elif op == 7:
                    break

                # Invalid Session
                elif op == 9:
                    break

    async def _heartbeat(
        self,
        ws,
        interval
    ):
        """
        Gateway Heartbeat
        """

        while self.running:

            await asyncio.sleep(interval)

            try:
                await ws.send(
                    json.dumps({
                        "op": 1,
                        "d": None
                    })
                )

            except Exception:
                break

    # =================================================
    # Gatewayイベント処理
    # =================================================

    async def _handle_event(
        self,
        event,
        data
    ):

        # Bot準備完了
        if event == "READY":

            self.user = data.get("user")

            await self.emit(
                "ready",
                self.user
            )

        # メッセージ
        elif event == "MESSAGE_CREATE":

            await self.emit(
                "message",
                data
            )

        # その他イベント
        elif event:

            await self.emit(
                event.lower(),
                data
            )

    # =================================================
    # 起動
    # =================================================

    async def start_async(self):

        if self.running:
            return

        self.running = True

        await self.emit("starting")

        while self.running:

            try:

                await self._gateway()

            except Exception as e:

                print(
                    f"[Discord Gateway Error] {e}"
                )

            if self.running:

                await asyncio.sleep(5)

    def start(self):
        """
        Botを起動する
        """

        asyncio.run(
            self.start_async()
        )

    # =================================================
    # 停止
    # =================================================

    def stop(self):

        self.running = False

        print(
            "[Discord] Bot stopped."
        )


# ====================================================
# 簡単なテスト用
# ====================================================

if __name__ == "__main__":

    TOKEN = "YOUR_BOT_TOKEN"

    bot = DiscordBot(TOKEN)

    def ready(user):
        print(
            f"Bot Ready: "
            f"{user.get('username')}"
        )

    def message(data):

        content = data.get(
            "content",
            ""
        )

        author = data.get(
            "author",
            {}
        )

        print(
            f"{author.get('username')}: "
            f"{content}"
        )

    bot.on(
        "ready",
        ready
    )

    bot.on(
        "message",
        message
    )

    bot.start()
