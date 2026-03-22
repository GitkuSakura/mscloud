import asyncio
import json
import mimetypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp


BASE_URL = "https://chat.monasa.net"


class TempCloudError(Exception):
    pass


class TempCloudAuthError(TempCloudError):
    pass


class TempCloudRequestError(TempCloudError):
    def __init__(self, status: int, message: str, payload: Optional[dict[str, Any]] = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload or {}


@dataclass(slots=True)
class TempCloudUploadResult:
    success: bool
    share_id: str
    share_url: str
    file_url: str
    filename: str
    content_type: str
    size: int
    expires_at: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TempCloudUploadResult":
        return cls(
            success=bool(payload.get("success")),
            share_id=str(payload.get("share_id", "")),
            share_url=str(payload.get("share_url", "")),
            file_url=str(payload.get("file_url", "")),
            filename=str(payload.get("filename", "")),
            content_type=str(payload.get("content_type", "")),
            size=int(payload.get("size", 0)),
            expires_at=str(payload.get("expires_at", "")),
        )


@dataclass(slots=True)
class _UploadJob:
    file_path: str | Path
    filename: Optional[str]
    content_type: Optional[str]
    retry_count: Optional[int]
    retry_interval_seconds: Optional[float]


class _TempCloudAsyncClient:
    def __init__(self, api_key: str, timeout_seconds: float = 180.0):
        self.base_url = BASE_URL
        self.api_key = api_key.strip()
        self.timeout_seconds = float(timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def upload_file(
        self,
        file_path: str | Path,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        retry_count: int = 1,
        retry_interval_seconds: float = 0.6,
    ) -> TempCloudUploadResult:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        resolved_filename = filename or path.name
        resolved_content_type = content_type or mimetypes.guess_type(resolved_filename)[0] or "application/octet-stream"
        retries = max(1, int(retry_count))
        interval = float(retry_interval_seconds)
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                with path.open("rb") as fp:
                    form = aiohttp.FormData()
                    form.add_field("file", fp, filename=resolved_filename, content_type=resolved_content_type)
                    payload = await self._post_json("/api/temp_cloud/v1/upload", form)
                    return TempCloudUploadResult.from_payload(payload)
            except (TempCloudAuthError, FileNotFoundError):
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(interval)
        if last_error:
            raise last_error
        raise TempCloudRequestError(500, "upload failed")

    async def _ensure_session(self):
        async with self._lock:
            if self._session and not self._session.closed:
                return
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _post_json(self, path: str, data: Any) -> dict[str, Any]:
        await self._ensure_session()
        assert self._session is not None
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key}
        async with self._session.post(url, data=data, headers=headers) as resp:
            return await self._handle_response(resp)

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        text = await resp.text()
        payload: dict[str, Any] = {}
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
        if resp.status == 401:
            raise TempCloudAuthError(payload.get("error") or text or "invalid api key")
        if resp.status >= 400:
            raise TempCloudRequestError(resp.status, payload.get("error") or text or "request failed", payload)
        if not payload:
            raise TempCloudRequestError(resp.status, "empty response")
        if payload.get("success") is False:
            raise TempCloudRequestError(resp.status, str(payload.get("error", "request failed")), payload)
        return payload


class MsCloudApp:
    def __init__(
        self,
        api_key: str,
        que_max: int = 3,
        timeout_seconds: float = 180.0,
        retry_count: int = 1,
        retry_interval_seconds: float = 0.6,
    ):
        self.api_key = api_key.strip()
        self.que_max = max(1, int(que_max))
        self.workers = self.que_max
        self.timeout_seconds = float(timeout_seconds)
        self.retry_count = max(1, int(retry_count))
        self.retry_interval_seconds = float(retry_interval_seconds)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._queue: Optional[asyncio.Queue] = None
        self._client: Optional[_TempCloudAsyncClient] = None
        self._workers_tasks: list[asyncio.Task] = []
        self._status: dict[str, str | int] = {}
        self._status_lock = threading.Lock()
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("mscloud bootstrap timeout")

    def load(
        self,
        file_path: str | Path,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        retry_count: Optional[int] = None,
        retry_interval_seconds: Optional[float] = None,
    ) -> None:
        if self._closed.is_set():
            raise RuntimeError("mscloud app is closed")
        key = str(file_path)
        with self._status_lock:
            self._status[key] = 0
        job = _UploadJob(
            file_path=file_path,
            filename=filename,
            content_type=content_type,
            retry_count=retry_count if retry_count is not None else self.retry_count,
            retry_interval_seconds=retry_interval_seconds if retry_interval_seconds is not None else self.retry_interval_seconds,
        )
        put_future = asyncio.run_coroutine_threadsafe(self._enqueue(job), self._loop)
        put_future.result()

    def get(self, file_path: str | Path) -> str | int:
        key = str(file_path)
        with self._status_lock:
            return self._status.pop(key, 0)

    def wait(self, timeout: Optional[float] = None) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._wait_all(), self._loop)
        fut.result(timeout=timeout)

    def close(self):
        if self._closed.is_set():
            return
        stop_future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
        stop_future.result()
        self._thread.join(timeout=5)
        self._closed.set()

    def _thread_main(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._bootstrap())
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _bootstrap(self):
        self._queue = asyncio.Queue(maxsize=self.que_max)
        self._client = _TempCloudAsyncClient(api_key=self.api_key, timeout_seconds=self.timeout_seconds)
        self._workers_tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        self._ready.set()

    async def _enqueue(self, job: _UploadJob):
        if not self._queue:
            raise RuntimeError("mscloud queue not initialized")
        await self._queue.put(job)

    async def _worker(self):
        if not self._queue or not self._client:
            return
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                result = await self._client.upload_file(
                    file_path=job.file_path,
                    filename=job.filename,
                    content_type=job.content_type,
                    retry_count=job.retry_count,
                    retry_interval_seconds=job.retry_interval_seconds,
                )
                with self._status_lock:
                    self._status[str(job.file_path)] = result.file_url or 0
            except Exception:
                with self._status_lock:
                    self._status[str(job.file_path)] = 0
            finally:
                self._queue.task_done()

    async def _wait_all(self):
        if self._queue:
            await self._queue.join()

    async def _shutdown_async(self):
        if not self._queue:
            self._loop.stop()
            return
        await self._queue.join()
        for _ in range(self.workers):
            await self._queue.put(None)
        await asyncio.gather(*self._workers_tasks, return_exceptions=True)
        if self._client:
            await self._client.close()
        self._loop.call_soon(self._loop.stop)


def app(api_key: str, que_max: int = 3, timeout_seconds: float = 180.0) -> MsCloudApp:
    return MsCloudApp(
        api_key=api_key,
        que_max=que_max,
        timeout_seconds=timeout_seconds,
    )


# 基本食用示例:
# import mscloud
#que_max:最大同时处理
# client = mscloud.app(api_key="xxx", que_max=3)
# client.load("path/to/file1.mp4")
# client.load("path/to/file2.zip")
# client.wait()
# print(client.get("path/to/file1.mp4"))
# print(client.get("path/to/file2.zip"))
# get会在读取后删除该键，避免重复获取
# client.close()

# 嵌入机器人主循环（伪代码 记得替换成你自己的方法）

# import mscloud
# c = mscloud.app(api_key="xxx", que_max=6)
# pending = {}  # {file_path: user_id}

# while True:
#     # 1) 从你的消息队列/请求池取“新上传请求集合”
#     new_reqs = fetch_new_upload_requests()
#     for req in new_reqs:
#         c.load(req.file_path)
#         pending[req.file_path] = req.user_id

#     # 2) 轮询完成结果，哪个先好就先发给对应用户
#     for path, user_id in list(pending.items()):
#         file_url = c.get(path)
#         if file_url != 0:
#             send_file_link_to_user(user_id, file_url)
#             pending.pop(path, None)

#     # 3) 你的主循环其余逻辑...
#     sleep(0.2)
