from __future__ import annotations
import asyncio
import concurrent.futures
import itertools
import logging
import multiprocessing
import os
import queue
import time
from typing import Any, Callable
import msgpack
from config import AUTH_BUNDLE_PATH
from maxapi_bootstrap import bootstrap_maxapi

bootstrap_maxapi()

from max_proto import MaxEvent, MaxPollingClient, MaxPollingRunner, MaxSdk
from max_proto.packet import MaxPacket

logger = logging.getLogger(__name__)


def _close_polling_client_without_ssl_shutdown(self: MaxPollingClient) -> None:
    tls = self.tls
    if tls is None:
        return
    self.tls = None
    try:
        fd = tls.detach()
    except OSError:
        fd = -1
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


MaxPollingClient.close = _close_polling_client_without_ssl_shutdown


def _max_polling_process_main(
    auth_bundle_path: str,
    chat_ids: list[int],
    command_queue: multiprocessing.Queue,
    event_queue: multiprocessing.Queue,
) -> None:
    logger = logging.getLogger(__name__)
    subscribed_chat_ids = set(chat_ids)

    def client_factory():
        polling_sdk = MaxSdk.from_auth_bundle(auth_bundle_path)
        return polling_sdk.create_polling_client()

    runner = MaxPollingRunner(
        client_factory=client_factory,
        chat_ids=subscribed_chat_ids,
        poll_timeout=10.0,
        max_events=5,
        idle_ping_interval=60.0,
    )

    def apply_pending_commands() -> None:
        while True:
            try:
                command, value = command_queue.get_nowait()
            except queue.Empty:
                return
            if command == "stop":
                runner.stop()
                return
            if command == "add_chat":
                try:
                    runner.add_chat(int(value))
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid Max chat subscription command value=%r", value)

    def on_event(event: MaxEvent) -> None:
        apply_pending_commands()
        event_queue.put(("event", event))

    def on_error(exc: Exception) -> None:
        apply_pending_commands()
        event_queue.put(("error", repr(exc)))

    try:
        runner.run_forever(on_event, on_error=on_error)
        event_queue.put(("stopped", None))
    except BaseException as exc:
        event_queue.put(("crash", repr(exc)))
        raise


def _max_sdk_worker_process_main(
    auth_bundle_path: str,
    request_queue: multiprocessing.Queue,
    response_queue: multiprocessing.Queue,
) -> None:
    sdk = MaxSdk.from_auth_bundle(auth_bundle_path)

    while True:
        request = request_queue.get()
        if not isinstance(request, tuple) or len(request) != 5:
            continue

        request_id, command, func_name, args, kwargs = request
        if command == "stop":
            response_queue.put((request_id, True, None))
            return

        try:
            if command == "sdk":
                func = getattr(sdk, func_name)
                result = func(*args, **kwargs)
            elif command == "client":
                func = getattr(sdk._client, func_name)
                result = func(*args, **kwargs)
            elif command == "bridge" and func_name == "get_contacts_by_ids":
                contact_ids = [int(item) for item in args[0] if item is not None]
                payload = msgpack.packb({"contactIds": contact_ids}, use_bin_type=True)
                packet = MaxPacket.build(
                    opcode=32,
                    payload=payload,
                    seq=sdk._client.standalone.session.next_seq(),
                )
                result = sdk._client.transact(packet)["response"]
            else:
                raise ValueError(f"Unknown SDK worker command: {command}")
            response_queue.put((request_id, True, result))
        except BaseException as exc:
            response_queue.put((request_id, False, repr(exc)))


class MaxBridge:
    def __init__(self, auth_bundle_path: str = str(AUTH_BUNDLE_PATH)):
        self.auth_bundle_path = auth_bundle_path
        self.sdk: MaxSdk | None = None
        self.is_running = False
        self.login_payload: dict[str, Any] | None = None
        self._on_event_callback: Callable[[MaxEvent], asyncio.Future] | None = None
        self._subscribed_chat_ids: set[int] = set()
        self._pending_chat_ids: set[int] = set()
        self._sdk_response_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="max-sdk-rpc",
        )
        self._mp_context = multiprocessing.get_context("spawn")
        self._sdk_process: multiprocessing.Process | None = None
        self._sdk_request_queue: multiprocessing.Queue | None = None
        self._sdk_response_queue: multiprocessing.Queue | None = None
        self._sdk_rpc_lock: asyncio.Lock | None = None
        self._sdk_request_ids = itertools.count(1)
        self._poll_process: multiprocessing.Process | None = None
        self._poll_command_queue: multiprocessing.Queue | None = None
        self._poll_event_queue: multiprocessing.Queue | None = None

    def set_on_event(self, callback: Callable[[MaxEvent], asyncio.Future]):
        self._on_event_callback = callback

    def is_authorized(self) -> bool:
        return AUTH_BUNDLE_PATH.exists()

    def load_sdk(self):
        if not self.is_authorized():
            return False

        self._start_sdk_process()
        try:
            self.login_payload = self._rpc_sdk_call_sync("sdk", "login", (), {})
            logger.info("Max SDK authorized and logged in.")
            return True
        except Exception as e:
            logger.error("Failed to login with existing bundle: %s", e)
            self._stop_sdk_process()
            return False

    async def _run_sdk_call(self, func_name: str, *args, **kwargs):
        return await self._rpc_sdk_call("sdk", func_name, args, kwargs)

    async def _run_sdk_client_call(self, func_name: str, *args, **kwargs):
        return await self._rpc_sdk_call("client", func_name, args, kwargs)

    async def get_contacts_by_ids(self, contact_ids: list[int]) -> list[dict]:
        unique_ids = sorted({int(item) for item in contact_ids if item is not None})
        if not unique_ids:
            return []
        response = await self._rpc_sdk_call("bridge", "get_contacts_by_ids", (unique_ids,), {})
        contacts = response.get("contacts") if isinstance(response, dict) else None
        return contacts if isinstance(contacts, list) else []

    def get_login_chats(self) -> list[dict]:
        if not isinstance(self.login_payload, dict):
            return []
        chats = self.login_payload.get("chats")
        return chats if isinstance(chats, list) else []

    def get_login_contacts(self) -> list[dict]:
        if not isinstance(self.login_payload, dict):
            return []
        contacts = self.login_payload.get("contacts")
        return contacts if isinstance(contacts, list) else []

    def get_own_contact_id(self) -> int | None:
        if not isinstance(self.login_payload, dict):
            return None
        profile = self.login_payload.get("profile")
        if not isinstance(profile, dict):
            return None
        contact = profile.get("contact")
        if not isinstance(contact, dict):
            return None
        contact_id = contact.get("id")
        return int(contact_id) if contact_id is not None else None

    async def start_polling(self, chat_ids: list[int] | None = None):
        self.is_running = True
        self._subscribed_chat_ids = set(chat_ids or [])
        logger.info("Starting Max polling...")
        reconnect_delay = 1.0

        while self.is_running:
            if not self.is_authorized():
                logger.error("Auth bundle is missing. Retrying Max polling startup in %.1fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
                continue

            try:
                self._start_poll_process()
                await self._consume_poll_events()
            except asyncio.CancelledError:
                self.is_running = False
                self._stop_poll_process()
                raise
            except Exception:
                logger.exception("Max polling supervisor failed; restarting polling process")
            finally:
                exitcode = self._stop_poll_process()
                if self.is_running:
                    logger.warning("Max polling process exited with code %s; restarting", exitcode)

            if not self.is_running:
                break
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)

    async def ensure_chat_subscription(self, chat_id: int) -> bool:
        if chat_id in self._subscribed_chat_ids or chat_id in self._pending_chat_ids:
            return False
        self._pending_chat_ids.add(chat_id)
        if self._poll_command_queue:
            self._poll_command_queue.put(("add_chat", chat_id))
            self._subscribed_chat_ids.add(chat_id)
            self._pending_chat_ids.discard(chat_id)
        logger.info("Queued subscription for Max chat %s", chat_id)
        return True

    def _start_sdk_process(self) -> None:
        if self._sdk_process is not None and self._sdk_process.is_alive():
            return

        self._stop_sdk_process()
        self._sdk_request_queue = self._mp_context.Queue()
        self._sdk_response_queue = self._mp_context.Queue()
        self._sdk_process = self._mp_context.Process(
            target=_max_sdk_worker_process_main,
            args=(
                self.auth_bundle_path,
                self._sdk_request_queue,
                self._sdk_response_queue,
            ),
            name="max-sdk-worker",
            daemon=True,
        )
        self._sdk_process.start()

    def _stop_sdk_process(self) -> int | None:
        process = self._sdk_process
        request_queue = self._sdk_request_queue
        response_queue = self._sdk_response_queue
        self._sdk_process = None
        self._sdk_request_queue = None
        self._sdk_response_queue = None

        if request_queue is not None:
            try:
                request_queue.put_nowait((0, "stop", "", (), {}))
            except Exception:
                pass

        exitcode = None
        if process is not None:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            exitcode = process.exitcode

        for mp_queue in (request_queue, response_queue):
            if mp_queue is None:
                continue
            try:
                mp_queue.close()
            except Exception:
                pass

        return exitcode

    def _rpc_sdk_call_sync(
        self,
        command: str,
        func_name: str,
        args: tuple,
        kwargs: dict,
        *,
        timeout: float = 180.0,
    ):
        self._start_sdk_process()
        process = self._sdk_process
        request_queue = self._sdk_request_queue
        response_queue = self._sdk_response_queue
        if process is None or request_queue is None or response_queue is None:
            raise RuntimeError("Max SDK worker process is not available")

        request_id = next(self._sdk_request_ids)
        request_queue.put((request_id, command, func_name, args, kwargs))
        deadline = time.monotonic() + timeout

        while True:
            if process.exitcode is not None:
                raise RuntimeError(f"Max SDK worker exited with code {process.exitcode}")
            try:
                response_id, success, payload = response_queue.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Max SDK worker call timed out: {func_name}")
                continue
            if response_id != request_id:
                continue
            if success:
                return payload
            raise RuntimeError(f"Max SDK worker call failed: {payload}")

    async def _rpc_sdk_call(
        self,
        command: str,
        func_name: str,
        args: tuple,
        kwargs: dict,
        *,
        timeout: float = 180.0,
    ):
        if self._sdk_rpc_lock is None:
            self._sdk_rpc_lock = asyncio.Lock()

        async with self._sdk_rpc_lock:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._rpc_sdk_call_sync,
                        command,
                        func_name,
                        args,
                        kwargs,
                        timeout=timeout,
                    ),
                    timeout=timeout + 5,
                )
            except RuntimeError as exc:
                if "Max SDK worker exited with code" not in str(exc):
                    raise
                logger.warning("Restarting Max SDK worker after crash during %s.%s", command, func_name)
                self._stop_sdk_process()
                self._start_sdk_process()
                if func_name != "login":
                    self.login_payload = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._rpc_sdk_call_sync,
                            "sdk",
                            "login",
                            (),
                            {},
                            timeout=timeout,
                        ),
                        timeout=timeout + 5,
                    )
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._rpc_sdk_call_sync,
                        command,
                        func_name,
                        args,
                        kwargs,
                        timeout=timeout,
                    ),
                    timeout=timeout + 5,
                )

    def _start_poll_process(self) -> None:
        self._poll_command_queue = self._mp_context.Queue()
        self._poll_event_queue = self._mp_context.Queue(maxsize=1000)
        self._poll_process = self._mp_context.Process(
            target=_max_polling_process_main,
            args=(
                self.auth_bundle_path,
                sorted(self._subscribed_chat_ids | self._pending_chat_ids),
                self._poll_command_queue,
                self._poll_event_queue,
            ),
            name="max-polling",
            daemon=True,
        )
        self._pending_chat_ids.clear()
        self._poll_process.start()

    def _stop_poll_process(self) -> int | None:
        process = self._poll_process
        command_queue = self._poll_command_queue
        event_queue = self._poll_event_queue
        self._poll_process = None
        self._poll_command_queue = None
        self._poll_event_queue = None

        if command_queue is not None:
            try:
                command_queue.put_nowait(("stop", None))
            except Exception:
                pass

        exitcode = None
        if process is not None:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            exitcode = process.exitcode

        for mp_queue in (command_queue, event_queue):
            if mp_queue is None:
                continue
            try:
                mp_queue.close()
            except Exception:
                pass

        return exitcode

    async def _consume_poll_events(self) -> None:
        event_queue = self._poll_event_queue
        process = self._poll_process
        if event_queue is None or process is None:
            raise RuntimeError("Max polling process was not started")

        while self.is_running:
            if process.exitcode is not None:
                return
            try:
                item_type, payload = await asyncio.to_thread(event_queue.get, True, 1.0)
            except queue.Empty:
                continue

            if item_type == "event":
                event = payload
                opcode = event.header.opcode
                if opcode not in (1, 292):
                    logger.info("Max event received: opcode=%s kind=%s", opcode, event.kind)
                if self._on_event_callback:
                    await self._on_event_callback(event)
            elif item_type == "error":
                logger.error("Error in Max polling process: %s", payload)
            elif item_type == "crash":
                logger.error("Max polling process crashed: %s", payload)
                return
            elif item_type == "stopped":
                return

    async def send_text(self, chat_id: int, text: str):
        return await self._run_sdk_call("send_text", chat_id=chat_id, text=text)

    async def get_chat_info(self, chat_id: int):
        return await self._run_sdk_call("get_chat_info", chat_id=chat_id)

    async def get_message(self, chat_id: int, message_id: int):
        return await self._run_sdk_call("get_message", chat_id, message_id)

    async def get_last_message(self, chat_id: int):
        return await self._run_sdk_call("get_last_message", chat_id)

    async def get_chat_history(self, **kwargs):
        return await self._run_sdk_call("get_chat_history", **kwargs)

    async def get_reactions(self, chat_id: int, message_ids: list[int]):
        return await self._run_sdk_call("get_reactions", chat_id, message_ids)

    async def list_chats(self):
        return await self._run_sdk_call("list_chats")

    async def download_photo(self, **kwargs):
        return await self._run_sdk_client_call("download_photo", **kwargs)

    async def download_video(self, **kwargs):
        return await self._run_sdk_client_call("download_video", **kwargs)

    async def resolve_video_urls(self, **kwargs):
        return await self._run_sdk_client_call("resolve_video_urls", **kwargs)

    async def download_audio(self, **kwargs):
        return await self._run_sdk_client_call("download_audio", **kwargs)

    async def download_file(self, **kwargs):
        return await self._run_sdk_client_call("download_file", **kwargs)

    async def download_sticker(self, **kwargs):
        return await self._run_sdk_client_call("download_sticker", **kwargs)

    async def send_local_photo(self, **kwargs):
        return await self._run_sdk_call("send_local_photo", **kwargs)

    async def send_local_video(self, **kwargs):
        return await self._run_sdk_call("send_local_video", **kwargs)

    async def send_local_file(self, **kwargs):
        return await self._run_sdk_call("send_local_file", **kwargs)

    async def edit_message(self, **kwargs):
        return await self._run_sdk_call("edit_message", **kwargs)
