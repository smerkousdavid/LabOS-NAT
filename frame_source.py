"""Video frame acquisition abstraction for STELLA VLM.

Implementations behind a common FrameSource interface:
  - WebSocketFrameSource     (request frames over WS)
  - RtspFrameSource          (pull from RTSP via OpenCV)
  - VideoStreamFrameSource   (read from WS video_stream ring buffer)
  - BufferedFrameSource      (reads from BackgroundFrameBuffer or PushFrameBuffer)

PushFrameBuffer accepts frames pushed over WebSocket into a timestamped
deque. get_frames() samples by timestamp -- no network round-trip.

BackgroundFrameBuffer (legacy) runs a background RTSP ingest loop.

Factory: create_frame_source(config, session_id)
"""

from __future__ import annotations

import asyncio
import base64
import collections
import io
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


# ---------------------------------------------------------------------------
# Background frame buffer (continuous RTSP ingest)
# ---------------------------------------------------------------------------

class BackgroundFrameBuffer:
    """Continuously pulls frames from RTSP into a timestamped ring buffer.

    Entries are ``(timestamp, base64_jpeg)`` tuples stored in a deque.
    Consumers call ``get_frames()`` which samples from the buffer by
    timestamp spacing -- no sleep or network call required.
    """

    _FPS = 5
    _MAXLEN = 300  # ~1 min at 5 fps
    _MAX_DIM = 384  # resize longest edge to keep VLM token count manageable

    def __init__(self, rtsp_url: str):
        self._rtsp_url = rtsp_url
        self._buf: collections.deque[Tuple[float, str]] = collections.deque(maxlen=self._MAXLEN)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._cap = None

    @property
    def size(self) -> int:
        return len(self._buf)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"[FrameBuffer] Started background capture from {self._rtsp_url}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        logger.info("[FrameBuffer] Stopped background capture")

    # -- frame retrieval -----------------------------------------------------

    def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        """Return *count* frames from the buffer spaced ~interval_ms apart.

        Samples by timestamp so the result set covers a time window of
        roughly ``count * interval_ms`` milliseconds.  Falls back to
        returning whatever is available if the buffer is sparse.
        """
        if not self._buf:
            return []

        snapshot = list(self._buf)

        if len(snapshot) <= count:
            return [frame for _, frame in snapshot]

        interval_sec = interval_ms / 1000.0
        latest_ts = snapshot[-1][0]
        target_start = latest_ts - interval_sec * (count - 1)

        selected: List[str] = []
        target_ts = target_start
        idx = 0
        for _ in range(count):
            best_idx = idx
            best_diff = abs(snapshot[idx][0] - target_ts)
            for j in range(idx + 1, len(snapshot)):
                diff = abs(snapshot[j][0] - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = j
                elif snapshot[j][0] > target_ts + interval_sec:
                    break
            selected.append(snapshot[best_idx][1])
            idx = best_idx + 1
            if idx >= len(snapshot):
                idx = len(snapshot) - 1
            target_ts += interval_sec

        return selected

    # -- internal poll loop --------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = 1.0 / self._FPS
        fail_streak = 0
        while not self._stop_event.is_set():
            try:
                frame_b64 = await asyncio.to_thread(self._read_one_frame)
                if frame_b64:
                    self._buf.append((time.monotonic(), frame_b64))
                    fail_streak = 0
                    await asyncio.sleep(interval)
                else:
                    fail_streak += 1
                    backoff = min(2.0 * fail_streak, 10.0)
                    if fail_streak == 1:
                        logger.info(f"[FrameBuffer] Stream not available, retrying every {backoff:.0f}s")
                    await asyncio.sleep(backoff)
            except Exception as exc:
                fail_streak += 1
                logger.debug(f"[FrameBuffer] Frame read error: {exc}")
                if self._cap is not None:
                    self._cap.release()
                    self._cap = None
                await asyncio.sleep(min(2.0 * fail_streak, 10.0))

    def _read_one_frame(self) -> Optional[str]:
        import cv2
        if self._cap is None or not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self._rtsp_url)
            if not self._cap.isOpened():
                return None
        ret, frame = self._cap.read()
        if not ret:
            self._cap.release()
            self._cap = None
            return None
        h, w = frame.shape[:2]
        if max(h, w) > self._MAX_DIM:
            scale = self._MAX_DIM / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
        _, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(jpeg_buf.tobytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Push-based frame buffer (receives frames over WebSocket)
# ---------------------------------------------------------------------------

class PushFrameBuffer:
    """Timestamped ring buffer fed by WebSocket ``video_stream`` messages.

    The runtime pushes base64 JPEG frames; ``ws_handler`` calls ``push()``
    for each one.  ``get_frames()`` samples the buffer by timestamp spacing,
    identical to ``BackgroundFrameBuffer``.
    """

    _FPS = 2
    _MAXLEN = 120  # ~1 min at 2 fps

    def __init__(self) -> None:
        self._buf: collections.deque[Tuple[float, str]] = collections.deque(maxlen=self._MAXLEN)

    @property
    def size(self) -> int:
        return len(self._buf)

    def push(self, frame_b64: str) -> None:
        """Append a base64-encoded JPEG with the current timestamp."""
        self._buf.append((time.monotonic(), frame_b64))

    async def stop(self) -> None:
        """Matches BackgroundFrameBuffer interface for cleanup."""
        self._buf.clear()

    def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        """Return *count* frames spaced ~interval_ms apart (by timestamp)."""
        if not self._buf:
            return []

        snapshot = list(self._buf)

        if len(snapshot) <= count:
            return [frame for _, frame in snapshot]

        interval_sec = interval_ms / 1000.0
        latest_ts = snapshot[-1][0]
        target_start = latest_ts - interval_sec * (count - 1)

        selected: List[str] = []
        target_ts = target_start
        idx = 0
        for _ in range(count):
            best_idx = idx
            best_diff = abs(snapshot[idx][0] - target_ts)
            for j in range(idx + 1, len(snapshot)):
                diff = abs(snapshot[j][0] - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = j
                elif snapshot[j][0] > target_ts + interval_sec:
                    break
            selected.append(snapshot[best_idx][1])
            idx = best_idx + 1
            if idx >= len(snapshot):
                idx = len(snapshot) - 1
            target_ts += interval_sec

        return selected


class FrameSource(ABC):
    """Abstract base for video frame acquisition."""

    @abstractmethod
    async def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        """Return `count` base64-encoded JPEG frames spaced interval_ms apart."""

    async def close(self):
        """Release resources."""
        pass


class WebSocketFrameSource(FrameSource):
    """Mode 1: Request frames over the session WebSocket."""

    def __init__(self, session_id: str):
        self._session_id = session_id

    async def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        from ws_handler import request_frames_from_runtime
        frames = await request_frames_from_runtime(
            self._session_id, count=count, interval_ms=interval_ms
        )
        return frames


class RtspFrameSource(FrameSource):
    """Mode 2/3: Pull frames from an RTSP URL via OpenCV.

    Keeps the VideoCapture connection alive between polls for efficiency.
    """

    def __init__(self, rtsp_url: str):
        self._rtsp_url = rtsp_url
        self._cap = None

    def _ensure_capture(self):
        if self._cap is None or not self._cap.isOpened():
            try:
                import cv2
                self._cap = cv2.VideoCapture(self._rtsp_url)
                if not self._cap.isOpened():
                    logger.warning(f"[FrameSource] Failed to open RTSP: {self._rtsp_url}")
            except ImportError:
                raise RuntimeError(
                    "OpenCV (cv2) is required for RTSP frame source. "
                    "Install with: pip install opencv-python-headless"
                )

    async def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        import cv2

        self._ensure_capture()
        if self._cap is None or not self._cap.isOpened():
            logger.warning("[FrameSource] RTSP capture not available, returning empty")
            return []

        frames: List[str] = []
        interval_sec = interval_ms / 1000.0

        for i in range(count):
            ret, frame = self._cap.read()
            if not ret:
                logger.warning(f"[FrameSource] Failed to read frame {i+1}/{count}")
                self._cap.release()
                self._cap = None
                break

            _, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            b64 = base64.b64encode(jpeg_buf.tobytes()).decode("ascii")
            frames.append(b64)

            if i < count - 1:
                await asyncio.sleep(interval_sec)

        return frames

    async def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class VideoStreamFrameSource(FrameSource):
    """Read frames from the WS video_stream ring buffer.

    The runtime pushes base64-encoded JPEG frames when FORWARD_FRAMES is
    enabled; ws_handler buffers them.  This source simply reads the latest
    frames from that buffer (no network round-trip required).
    """

    def __init__(self, session_id: str):
        self._session_id = session_id

    async def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        from ws_handler import get_latest_ws_frames
        return get_latest_ws_frames(self._session_id, count)


class BufferedFrameSource(FrameSource):
    """Reads from a running frame buffer (push or background) -- instant return."""

    def __init__(self, buffer: "BackgroundFrameBuffer | PushFrameBuffer"):
        self._buffer = buffer

    async def get_frames(self, count: int = 8, interval_ms: int = 1250) -> List[str]:
        return self._buffer.get_frames(count, interval_ms)


def create_frame_source(config: Dict[str, Any], session_id: str) -> FrameSource:
    """Factory: create the appropriate FrameSource based on config.

    If a BackgroundFrameBuffer is running for this session it is used
    automatically (instant return, no network call).
    """
    from ws_handler import get_frame_buffer
    buf = get_frame_buffer(session_id)
    if buf is not None:
        return BufferedFrameSource(buf)

    video_cfg = config.get("video", {})
    mode = video_cfg.get("mode", "websocket")

    if mode == "websocket":
        return WebSocketFrameSource(session_id)

    elif mode == "rtsp_pull":
        rtsp_url = _build_rtsp_url(video_cfg, session_id)
        return RtspFrameSource(rtsp_url)

    elif mode == "mediamtx_relay":
        local_port = video_cfg.get("local_mediamtx_port", 8654)
        from ws_handler import get_stream_info
        info = get_stream_info(session_id)
        path = _video_path_from_stream_info(info)
        rtsp_url = f"rtsp://localhost:{local_port}/{path}"
        return RtspFrameSource(rtsp_url)

    elif mode == "video_stream":
        return VideoStreamFrameSource(session_id)

    else:
        logger.warning(f"[FrameSource] Unknown mode '{mode}', falling back to rtsp_pull")
        rtsp_url = _build_rtsp_url(video_cfg, session_id)
        return RtspFrameSource(rtsp_url)


def _video_path_from_stream_info(info: Optional[Dict[str, Any]]) -> str:
    """Extract the video sub-path from a stream_info dict."""
    if info:
        return info.get("paths", {}).get("video", "NB_0001_TX_CAM_RGB")
    return "NB_0001_TX_CAM_RGB"


def _build_rtsp_url(video_cfg: Dict[str, Any], session_id: str) -> str:
    """Build a full RTSP URL from config + stream_info.

    Prefers the ``rtsp_base`` advertised via ``stream_info`` over the
    static ``mediamtx_url`` in ``config.yaml`` so that the runtime can
    announce the correct externally-reachable address.
    """
    from ws_handler import get_stream_info
    info = get_stream_info(session_id)
    path = _video_path_from_stream_info(info)

    if info and info.get("rtsp_base"):
        base = info["rtsp_base"].rstrip("/")
    else:
        base = video_cfg.get("mediamtx_url", "rtsp://localhost:8554").rstrip("/")

    return f"{base}/{path}"
