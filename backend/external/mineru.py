"""MinerU API client（PDF 解析）。

支持两种模式（由 config.toml [mineru] mode 决定）：
  - "local": 本机 Docker 部署的 MinerU API（/file_parse 同步接口，直接返回 markdown）
  - "cloud":  MinerU 官方云 API v4（需 MINERU_TOKEN，走 file-urls/batch + OSS 上传 + 轮询 + ZIP 下载）

云 API 不继承 RateLimitedClient：
  - 解析任务粒度大（分钟级），磁盘缓存无意义
  - 需上传二进制 / 下载 ZIP，与 RateLimitedClient 的 JSON 缓存机制不兼容
  - 改用内置的 Session 复用 + 软限频（submit 5 任务/min）

本地 API 无需限频（本机服务），直接同步 POST 文件即返回结果。
"""
import io
import queue
import threading
import time
from collections import deque
from pathlib import Path
import zipfile

import requests

from backend.config import CONFIG
from backend.services.mineru_health import mineru_file_lock


_MODE = CONFIG["mineru"].get("mode", "cloud")


class MinerUClient:
    """MinerU 解析 client。

    mode="cloud": Session 复用 + 软限频（submit 5/min）。
    mode="local": 无需限频，直接 POST 文件到本地 /file_parse。
    """

    api_base = CONFIG["mineru"]["api_base"]
    local_api_base = CONFIG["mineru"].get("local_api_base", "http://127.0.0.1:8000")

    # 类级令牌窗口（submit 5 任务/min），跨实例共享避免并发 worker 超限
    _submit_window: deque = deque()
    _submit_lock = threading.Lock()
    SUBMIT_MAX_PER_MIN = 5

    def __init__(self) -> None:
        self._mode = _MODE
        if self._mode == "cloud":
            self._token = CONFIG["secrets"]["mineru_token"]
            if not self._token:
                raise RuntimeError("MINERU_TOKEN missing in env")
            self._session = requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self._token}"})
        else:
            self._session = requests.Session()

    def _throttle_submit(self) -> None:
        """submit 5 req/min 软限频。窗口已满则 sleep 到最早请求过期。"""
        with self._submit_lock:
            now = time.time()
            while self._submit_window and now - self._submit_window[0] >= 60:
                self._submit_window.popleft()
            if len(self._submit_window) >= self.SUBMIT_MAX_PER_MIN:
                wait = 60 - (now - self._submit_window[0]) + 0.5
                time.sleep(max(0.0, wait))
                now = time.time()
                while self._submit_window and now - self._submit_window[0] >= 60:
                    self._submit_window.popleft()
            self._submit_window.append(now)

    # ── v4 精准 API ────────────────────────────────────
    def submit_v4(self, pdf_path: str, model: str = "pipeline", is_ocr: bool = False, lang: str = "en") -> str:
        """提交解析任务，返回 task_id。流程：上传到 OSS → 创建 task。"""
        self._throttle_submit()
        # Step 1: 申请 OSS 上传 URL
        r = self._session.post(
            f"{self.api_base}/file-urls/batch",
            json={
                "files": [{"name": Path(pdf_path).name, "is_ocr": is_ocr, "data_id": Path(pdf_path).stem}],
                "language": lang,
                "model_version": model,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls") or []
        if not batch_id or not upload_urls:
            raise RuntimeError(f"MinerU file-urls bad response: {r.json()}")

        # Step 2: 上传文件到 OSS（OSS 不需要 Authorization 头，用独立 put 调用）
        with open(pdf_path, "rb") as f:
            up = requests.put(upload_urls[0], data=f, timeout=120)
            up.raise_for_status()

        return batch_id

    def poll_v4(self, batch_id: str, timeout: int = 600, interval: float = 5.0) -> dict:
        """轮询任务结果。返回完整 result dict。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self._session.get(
                f"{self.api_base}/extract-results/batch/{batch_id}",
                timeout=30,
            )
            r.raise_for_status()
            data = r.json().get("data") or {}
            results = data.get("extract_result") or []
            if results and all(it.get("state") in ("done", "failed") for it in results):
                return data
            time.sleep(interval)
        raise TimeoutError(f"MinerU poll timeout after {timeout}s for batch={batch_id}")

    def download_zip(self, full_zip_url: str, target_dir: Path) -> Path:
        """下载并解压结果 ZIP。"""
        r = self._session.get(full_zip_url, timeout=120)
        r.raise_for_status()
        target_dir.mkdir(parents=True, exist_ok=True)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        zf.extractall(target_dir)
        return target_dir

    # ── 本地 Docker API（/file_parse 同步接口）────────
    def parse_pdf_local(self, pdf_path: str, output_dir: Path) -> dict:
        """本地解析期间持有共享锁，阻止定时检查脚本同时重启容器。"""
        with mineru_file_lock(exclusive=False):
            return self._parse_pdf_local_locked(pdf_path, output_dir)

    def _parse_pdf_local_locked(self, pdf_path: str, output_dir: Path) -> dict:
        """调用本地 /file_parse，并写入 markdown 与图片。"""
        import base64

        backend = CONFIG["mineru"].get("local_backend", "hybrid-engine")
        effort = CONFIG["mineru"].get("local_effort", "medium")
        pdf_name = Path(pdf_path).name

        timeout = CONFIG["mineru"].get("local_timeout", 1800)
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def _request() -> None:
            try:
                with open(pdf_path, "rb") as f:
                    response = self._session.post(
                        f"{self.local_api_base}/file_parse",
                        data={
                            "backend": backend,
                            "effort": effort,
                            "parse_method": "auto",
                            "return_md": "true",
                            "return_images": "true",
                            "response_format_zip": "false",
                        },
                        files={"files": (pdf_name, f, "application/pdf")},
                        timeout=timeout,
                    )
                result_q.put((True, response))
            except Exception as exc:  # noqa: BLE001
                result_q.put((False, exc))

        request_thread = threading.Thread(
            target=_request, daemon=True, name="mineru-local-request",
        )
        request_thread.start()
        request_thread.join(timeout)
        if request_thread.is_alive():
            raise TimeoutError(
                f"local MinerU /file_parse exceeded total timeout {timeout}s"
            )
        ok, value = result_q.get_nowait()
        if not ok:
            raise value
        r = value
        if r.status_code != 200:
            error_text = r.text
            try:
                payload = r.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("error"):
                error_text = str(payload["error"])
            markers = (
                "device string must not be empty",
                "failed to initialize nvml",
                "nvml initialization failed",
                "nvml: unknown error",
            )
            if any(marker in error_text.lower() for marker in markers):
                container = CONFIG["mineru"].get("local_container", "")
                location = f"container '{container}'" if container else "local container"
                restart = f"docker restart {container}" if container else "restart the local MinerU container"
                raise RuntimeError(
                    f"local MinerU GPU unavailable in {location}: "
                    f"HTTP {r.status_code}: {r.text[:500]}. "
                    f"Restart it with: {restart}; then retry the task."
                )
            raise RuntimeError(
                f"local MinerU /file_parse HTTP {r.status_code}: {r.text[:500]}"
            )
        data = r.json()

        if data.get("status") != "completed":
            raise RuntimeError(
                f"local MinerU parse failed: status={data.get('status')} "
                f"error={data.get('error')}"
            )
        results = data.get("results") or {}
        if not results:
            raise RuntimeError("local MinerU returned empty results")
        inner = next(iter(results.values()))

        output_dir.mkdir(parents=True, exist_ok=True)
        images_dir = output_dir / "images"

        # 落盘图片（base64 data URI -> 文件）
        images = inner.get("images") or {}
        if images:
            images_dir.mkdir(parents=True, exist_ok=True)
            for img_name, data_uri in images.items():
                if not isinstance(data_uri, str) or "," not in data_uri:
                    continue
                b64 = data_uri.split(",", 1)[1]
                (images_dir / img_name).write_bytes(base64.b64decode(b64))

        # 落盘 markdown
        md_content = inner.get("md_content") or ""
        full_md = output_dir / "full.md"
        full_md.write_text(md_content, encoding="utf-8")

        return {
            "full_md": full_md,
            "images_dir": images_dir if images_dir.exists() and any(images_dir.iterdir()) else None,
            "model": backend,
            "chars": len(md_content),
        }

    # ── 综合 API（统一入口，按 mode 分发）──────────────
    def parse_pdf(self, pdf_path: str, output_dir: Path, model: str = "pipeline", is_ocr: bool = False) -> dict:
        """端到端解析：返回 {full_md_path, images_dir, model, chars}

        mode="local": 走本地 /file_parse（忽略 model/is_ocr 参数，由 config 后端配置决定）
        mode="cloud": 走云 API v4（OSS 上传 + 轮询 + ZIP 下载）
        """
        if self._mode == "local":
            return self.parse_pdf_local(pdf_path, output_dir)
        return self._parse_pdf_cloud(pdf_path, output_dir, model=model, is_ocr=is_ocr)

    def _parse_pdf_cloud(self, pdf_path: str, output_dir: Path, model: str = "pipeline", is_ocr: bool = False) -> dict:
        batch_id = self.submit_v4(pdf_path, model=model, is_ocr=is_ocr)
        result = self.poll_v4(batch_id)
        items = result.get("extract_result") or []
        if not items:
            raise RuntimeError("MinerU returned empty extract_result")
        item = items[0]
        if item.get("state") != "done":
            raise RuntimeError(f"MinerU failed: {item.get('err_msg', 'unknown')}")
        zip_url = item.get("full_zip_url")
        if not zip_url:
            raise RuntimeError("MinerU response missing full_zip_url")
        self.download_zip(zip_url, output_dir)
        # 找出 full.md 与 images 目录
        full_md = next(output_dir.rglob("full.md"), None)
        if not full_md:
            raise RuntimeError(f"MinerU output missing full.md under {output_dir}")
        images = full_md.parent / "images"
        return {
            "full_md": full_md,
            "images_dir": images if images.exists() else None,
            "model": model,
            "chars": len(full_md.read_text(encoding="utf-8", errors="replace")),
        }