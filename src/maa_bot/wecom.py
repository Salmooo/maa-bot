"""企业微信「智能机器人 - 长连接 API 模式」客户端。

文档: https://developer.work.weixin.qq.com/document/path/101463
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

import requests
import websockets

log = logging.getLogger("wecom")

Handler = Callable[[dict], Awaitable[None]]
SENSITIVE_KEYS = {"authorization", "secret", "token", "response_url", "api_key"}


def redact(value: Any) -> Any:
    """Remove credentials from values written to logs."""
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in SENSITIVE_KEYS else redact(item)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _safe_json(value: Any, limit: int = 400) -> str:
    text = json.dumps(redact(value), ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


class WeComBot:
    def __init__(
        self,
        bot_id: str,
        secret: str,
        url: str = "wss://openws.work.weixin.qq.com",
        heartbeat: int = 25,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.url = url
        self.heartbeat = heartbeat
        self.handlers: list[Handler] = []
        self.connected = False
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._stop = asyncio.Event()
        self.on_connect: Callable[[], None] | None = None
        self.last_rx = 0.0
        self._session_task: asyncio.Task | None = None
        self._kicked = False

    # ---------------- 事件注册 ----------------

    def on_message(self, handler: Handler) -> Handler:
        self.handlers.append(handler)
        return handler

    async def _dispatch(self, frame: dict) -> None:
        for handler in self.handlers:
            try:
                await handler(frame)
            except Exception:  # noqa: BLE001
                log.exception("消息处理失败")

    # ---------------- 连接管理 ----------------

    async def run(self) -> None:
        """保持长连接，断线自动重连（指数退避）。"""
        backoff = 3
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    max_size=8 * 1024 * 1024,
                    open_timeout=20,
                    close_timeout=3,
                ) as ws:
                    self._ws = ws
                    if not await self._subscribe(ws):
                        raise RuntimeError("订阅失败")
                    self.connected = True
                    self._kicked = False
                    backoff = 3
                    log.info("长连接已建立")
                    if self.on_connect:
                        self.on_connect()
                    self._session_task = asyncio.create_task(self._session(ws))
                    try:
                        await self._session_task
                    except asyncio.CancelledError:
                        if self._stop.is_set() or not self._kicked:
                            raise
                        log.info("主动重建长连接")
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
            except Exception as exc:  # noqa: BLE001
                log.warning("连接异常：%s", exc)
            finally:
                self.connected = False
                # 半死连接可能连 close 都卡住，直接掐掉 TCP 保证能重连
                with contextlib.suppress(Exception):
                    transport = getattr(self._ws, "transport", None)
                    if transport is not None:
                        transport.abort()
                self._ws = None
                self._session_task = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def _subscribe(self, ws: Any) -> bool:
        req_id = uuid.uuid4().hex
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        }
        await ws.send(json.dumps(payload, ensure_ascii=False))
        log.info("已发送订阅请求")
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            frame = _loads(raw)
            if not frame:
                continue
            if frame.get("headers", {}).get("req_id") == req_id:
                log.info("订阅响应：%s", _safe_json(frame))
                return frame.get("errcode", -1) == 0
            await self._handle(frame)
        return False

    async def _session(self, ws: Any) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                frame = _loads(raw)
                if frame:
                    await self._handle(frame)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            req_id: str | None = None
            try:
                req_id = uuid.uuid4().hex
                fut = asyncio.get_running_loop().create_future()
                self._pending[req_id] = fut
                await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": req_id}}))
                await asyncio.wait_for(fut, timeout=10)
                log.debug("心跳正常")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("心跳失败：%s", exc)
                await self.force_reconnect()
                return
            finally:
                self._pending.pop(req_id, None)

    async def _handle(self, frame: dict) -> None:
        self.last_rx = time.time()
        headers = frame.get("headers") or {}
        req_id = headers.get("req_id")
        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(frame)
            return

        cmd = frame.get("cmd") or ""
        body = frame.get("body") or {}
        if cmd == "aibot_event_callback":
            event_type = (body.get("event") or {}).get("eventtype") or ""
            if event_type == "disconnected_event":
                raise RuntimeError("连接已被新的长连接替换")
        if cmd == "ping":
            await self._raw({"cmd": "pong", "headers": {"req_id": uuid.uuid4().hex}})
            return
        if cmd and cmd != "aibot_msg_callback" and any(k in cmd.lower() for k in ("disconnect", "kick")):
            raise RuntimeError(f"服务端主动断开：{cmd}")

        if cmd:
            log.info("<< %s", _safe_json(frame))
            await self._dispatch(frame)

    async def _raw(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("长连接未建立")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _request(self, payload: dict, timeout: float = 15.0) -> dict | None:
        if self._ws is None:
            raise RuntimeError("长连接未建立")
        req_id = uuid.uuid4().hex
        payload.setdefault("headers", {})["req_id"] = req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._raw(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("请求超时：%s", _safe_json(payload, 200))
            return None
        finally:
            self._pending.pop(req_id, None)

    # ---------------- 主动推送 ----------------

    async def send_markdown(self, chat_id: str, chat_type: int, content: str) -> dict | None:
        body: dict[str, Any] = {
            "chatid": chat_id,
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        if chat_type:
            body["chat_type"] = chat_type
        resp = await self._request({"cmd": "aibot_send_msg", "body": body})
        if resp and resp.get("errcode") == 0:
            log.info("推送成功 -> %s", chat_id)
        else:
            log.warning("推送失败：%s", _safe_json(resp) if resp else "无响应")
        return resp

    async def reply_markdown(self, callback_req_id: str, content: str) -> dict | None:
        """被动回复：用消息回调里的 req_id 回一条 markdown（长连接模式推荐方式）。"""
        if not callback_req_id or self._ws is None:
            return None
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": callback_req_id},
            "body": {"msgtype": "markdown", "markdown": {"content": content}},
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[callback_req_id] = fut
        try:
            await self._raw(payload)
            resp = await asyncio.wait_for(fut, timeout=8)
        except asyncio.TimeoutError:
            log.warning("被动回复超时")
            resp = None
        except Exception as exc:  # noqa: BLE001
            log.warning("被动回复异常：%s", exc)
            resp = None
        finally:
            self._pending.pop(callback_req_id, None)
        if resp and resp.get("errcode") == 0:
            log.info("被动回复成功")
        else:
            log.info("被动回复未成功：%s", _safe_json(resp) if resp else "无响应")
        return resp

    async def reply_via_url(self, response_url: str, content: str) -> bool:
        """用回调里的 response_url 走 HTTP 回复（长连接半死时的兜底通道）。"""
        if not response_url:
            return False
        body = {"msgtype": "markdown", "markdown": {"content": content}}
        candidates = [
            body,
            {"cmd": "aibot_respond_msg", "headers": {}, "body": body},
        ]
        for index, payload in enumerate(candidates, 1):
            try:
                resp = await asyncio.to_thread(requests.post, response_url, json=payload, timeout=12)
            except Exception as exc:  # noqa: BLE001
                log.warning("response_url 回复异常（格式%d）：%s", index, exc)
                continue
            try:
                data = resp.json() if resp.content else {}
            except Exception:  # noqa: BLE001
                data = {}
            if resp.status_code == 200 and int(data.get("errcode", 0) or 0) == 0:
                log.info("response_url 回复成功（格式%d）", index)
                return True
            log.info("response_url 回复失败（格式%d）：%s %s", index, resp.status_code, (resp.text or "")[:140])
        return False

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.connected:
                return True
            await asyncio.sleep(0.5)
        return self.connected

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def force_reconnect(self) -> None:
        """主动断开当前连接，触发重连（用于半死连接）。"""
        self._kicked = True
        task = self._session_task
        if task is not None and not task.done():
            task.cancel()
            return
        if self._ws is not None:
            with contextlib.suppress(Exception):
                transport = getattr(self._ws, "transport", None)
                if transport is not None:
                    transport.abort()


def _loads(raw: Any) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        frame = json.loads(raw)
    except Exception:  # noqa: BLE001
        log.debug("无法解析的帧：%s", str(raw)[:200])
        return None
    return frame if isinstance(frame, dict) else None


async def send_message_once(
    cfg: dict,
    content: str,
    chat_id: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """一次性连接并推送一条消息（用完即断，适合脚本场景）。"""
    bot = WeComBot(
        cfg["bot_id"],
        cfg["secret"],
        cfg.get("ws_url", "wss://openws.work.weixin.qq.com"),
        int(cfg.get("heartbeat_seconds", 25)),
    )
    task = asyncio.create_task(bot.run())
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and not bot.connected:
            await asyncio.sleep(0.5)
        if not bot.connected:
            raise RuntimeError("长连接未建立")
        target = chat_id or cfg.get("chat_id")
        if not target:
            raise RuntimeError("未配置 chat_id")
        return await bot.send_markdown(target, int(cfg.get("chat_type") or 0), content)
    finally:
        await bot.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
