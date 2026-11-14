"""Local MQTT broker and detection subscription for the Ubuntu desktop."""
import asyncio
import threading


class DetectionReceiver:
    def __init__(self, logs, port=18830):
        self.logs = logs
        self.port = port
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="MQTT detection", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self):
        try:
            asyncio.run(self._receive())
        except Exception as exc:
            self.logs.write("MQTT", f"接收服务失败：{exc}")

    async def _receive(self):
        from amqtt.broker import Broker
        from amqtt.client import MQTTClient

        broker = Broker({
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{self.port}"}},
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
        })
        await broker.start()
        client = MQTTClient()
        try:
            await asyncio.wait_for(client.connect(f"mqtt://127.0.0.1:{self.port}"), timeout=3)
            await asyncio.wait_for(client.subscribe([("desk/#", 0)]), timeout=3)
            self.logs.write("MQTT", f"已订阅 desk/#，本机 Broker 监听 127.0.0.1:{self.port}")
            while not self.stop_event.is_set():
                try:
                    message = await client.deliver_message(timeout_duration=0.4)
                except TimeoutError:
                    continue
                if message:
                    payload = bytes(message.data).decode("utf-8", errors="replace")
                    self.logs.write("MQTT", f"{message.topic}  {payload}")
        finally:
            try:
                if client.session and client.session.transitions.is_connected():
                    await asyncio.wait_for(client.disconnect(), timeout=2)
            finally:
                await asyncio.wait_for(broker.shutdown(), timeout=2)
