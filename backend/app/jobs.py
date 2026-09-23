"""Serial, restart-aware local worker. Only the API process publishes revisions."""
import asyncio
import hashlib
import logging
import multiprocessing
import shutil
import time
import uuid
from pathlib import Path

from .store import decode, encode, now
from .preview_cache import preview_signature

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class JobConflict(ValueError):
    pass


def drawing_root(settings, drawing_id: str) -> Path:
    return settings.data_dir / "drawings" / drawing_id


def render_key(layout: str, bbox=None, settings=None) -> str:
    return hashlib.sha256(encode([layout, bbox, preview_signature(settings)]).encode()).hexdigest()[:24]


def worker_entry(connection, kind: str, source: str, output: str, payload: dict, settings: dict):
    """Runs in a fresh process so CAD memory and timeout failures stay isolated."""
    try:
        from .cad import import_drawing, render_drawing
        if kind == "import":
            result = import_drawing(source, output, settings)
        elif kind == "restore":
            result = import_drawing(source, output, settings)
        elif kind == "render":
            result = render_drawing(source, output, payload.get("layout", "Model"), payload.get("bbox"), settings)
            for extension in ("svg", "png"):
                artifact = Path(output) / result["artifacts"][extension]
                stable = Path(output) / f"preview.{extension}"
                if artifact != stable:
                    artifact.rename(stable)
                result["artifacts"][extension] = stable.name
        elif kind == "export":
            from .cad import export_drawing
            result = export_drawing(source, output, "dwg", settings)
        else:
            raise ValueError("Unsupported job kind")
        connection.send({"ok": True, "result": result})
    except Exception as exc:
        message = str(exc)
        # Converter paths are service details, not part of product-facing errors.
        for p in (source, output, settings.get("oda_executable", "")):
            if p:
                message = message.replace(p, "[图纸文件]")
        connection.send({"ok": False, "code": getattr(exc, "code", type(exc).__name__), "error": message[:1200]})
    finally:
        connection.close()


class JobManager:
    def __init__(self, settings, store, bus):
        self.settings, self.store, self.bus = settings, store, bus
        self.queue = asyncio.Queue()
        self.runner = None
        self.context = multiprocessing.get_context("spawn")

    def start(self):
        self.runner = asyncio.create_task(self._loop())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            try:
                await self.runner
            except asyncio.CancelledError:
                pass

    def get(self, job_id):
        row = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row:
            row.pop("payload", None)
        return row

    def submit(self, drawing_id, kind, payload, session_id=None, operation_id=None):
        if kind not in {"import", "restore", "render", "export"}:
            raise ValueError("旧 CAD 操作已移除；编辑请通过 FreeCAD 智能体执行。")
        job_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            drawing = db.execute("SELECT * FROM drawings WHERE id=?", (drawing_id,)).fetchone()
            if not drawing:
                raise ValueError("图纸不存在。")
            if operation_id:
                old = decode(db.execute("SELECT * FROM jobs WHERE drawing_id=? AND operation_id=?", (drawing_id, operation_id)).fetchone())
                if old:
                    if old["payload"] != payload or old["kind"] != kind or old["session_id"] != session_id:
                        raise JobConflict("操作编号已被不同请求使用。")
                    return self.get(old["id"])
            if kind in {"restore"} and drawing["current_revision"] != payload.get("expected_revision"):
                raise JobConflict("图纸已有新版本，请刷新后重新提交。")
            if session_id:
                session = db.execute("SELECT * FROM sessions WHERE id=? AND drawing_id=?", (session_id, drawing_id)).fetchone()
                if not session or session["busy"] != 1:
                    raise JobConflict("会话任务已经停止或不属于此图纸。")
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, drawing_id, session_id, kind, "queued", encode(payload), None, None, now(), now(), operation_id))
        self.queue.put_nowait(job_id)
        self._emit(job_id)
        return self.get(job_id)

    def cancel(self, job_id):
        self.store.execute("UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,updated_at=? WHERE id=? AND status IN ('queued','running')", (now(), job_id))
        job = self.get(job_id)
        if job["kind"] == "import" and job["status"] == "cancelled":
            self.store.execute("UPDATE drawings SET status='failed' WHERE id=? AND current_revision IS NULL", (job["drawing_id"],))
        self._emit(job_id)
        return self.get(job_id)

    def cancel_session(self, session_id):
        jobs = self.store.all("SELECT id FROM jobs WHERE session_id=? AND status IN ('queued','running','cancelling')", (session_id,))
        for job in jobs:
            self.cancel(job["id"])
        return [self.get(job["id"]) for job in jobs]

    async def wait(self, job_id):
        while True:
            job = self.get(job_id)
            if job["status"] in TERMINAL:
                return job
            await asyncio.sleep(0.15)

    def _emit(self, job_id):
        job = self.get(job_id)
        self.bus.publish({"type": "job.updated", "drawing_id": job["drawing_id"], "job": job})

    def _finish(self, job_id, status, result=None, error=None):
        self.store.execute("UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?", (status, encode(result) if result is not None else None, error, now(), job_id))
        job = self.get(job_id)
        if job["kind"] == "import" and status in {"failed", "cancelled", "interrupted"}:
            self.store.execute("UPDATE drawings SET status='failed' WHERE id=? AND current_revision IS NULL", (job["drawing_id"],))
        self._emit(job_id)

    async def _loop(self):
        while True:
            job_id = await self.queue.get()
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                if self.get(job_id)["status"] not in TERMINAL:
                    self._finish(job_id, "interrupted", error="服务已停止，任务未发布。")
                raise
            except Exception:
                logger.exception("CAD job failed: %s", job_id)
                self._finish(job_id, "failed", error="任务处理失败，请查看服务日志并重试。")
            finally:
                self.queue.task_done()

    async def _run(self, job_id):
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job["status"] != "queued":
            return
        payload, kind, drawing_id = job["payload"], job["kind"], job["drawing_id"]
        drawing = self.store.one("SELECT * FROM drawings WHERE id=?", (drawing_id,))
        root = drawing_root(self.settings, drawing_id)
        revision_id = payload.get("revision_id") or payload.get("expected_revision")
        if kind == "import":
            source = root / "source" / ("original" + drawing["source_suffix"])
        else:
            if kind in {"restore"} and drawing["current_revision"] != payload["expected_revision"]:
                self._finish(job_id, "failed", error="版本冲突：图纸已更新，请重新查询后修改。")
                return
            source = root / "revisions" / revision_id / "drawing.dxf"
        output = self.settings.data_dir / "tasks" / job_id
        output.mkdir(parents=True, exist_ok=True)
        self.store.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job_id))
        self._emit(job_id)
        parent, child = self.context.Pipe(duplex=False)
        process = self.context.Process(target=worker_entry, args=(child, kind, str(source), str(output), payload, self.settings.cad_settings()))
        process.start()
        child.close()
        started = time.monotonic()
        response = None
        try:
            while response is None:
                if self.get(job_id)["status"] == "cancelling":
                    self._finish(job_id, "cancelled")
                    return
                if time.monotonic() - started > self.settings.cad_job_timeout:
                    self._finish(job_id, "failed", error="图纸处理超时，上一成功版本已保留。")
                    return
                if parent.poll():
                    try:
                        response = parent.recv()
                    except EOFError:
                        response = {"ok": False, "error": "CAD 工作进程异常退出。"}
                elif not process.is_alive():
                    response = {"ok": False, "error": "CAD 工作进程异常退出。"}
                else:
                    await asyncio.sleep(0.1)
            if not response["ok"]:
                self._finish(job_id, "failed", error=response["error"])
                if kind == "import":
                    self.store.execute("UPDATE drawings SET status='failed' WHERE id=?", (drawing_id,))
                return
            if self.get(job_id)["status"] == "cancelling":
                self._finish(job_id, "cancelled")
                return
            result = response["result"]
            if kind in {"import", "restore"}:
                result = self._publish(job, output, result)
                if result is None:
                    return
            elif kind == "render":
                key = render_key(payload.get("layout", "Model"), payload.get("bbox"), self.settings.cad_settings())
                target = root / "previews" / revision_id / key
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    output.rename(target)
                result["preview_url"] = f"/api/drawings/{drawing_id}/revisions/{revision_id}/preview?format=png&render_id={key}"
            elif kind == "export":
                target = root / "exports" / revision_id
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output / "drawing.dwg", target / "drawing.dwg")
                result["download_url"] = f"/api/drawings/{drawing_id}/revisions/{revision_id}/download?format=dwg"
            self._finish(job_id, "succeeded", result)
        finally:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 3)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1)
            parent.close()
            if output.exists():
                shutil.rmtree(output)

    def _publish(self, job, output, metadata):
        drawing_id, payload = job["drawing_id"], job["payload"]
        root = drawing_root(self.settings, drawing_id)
        with self.store.transaction() as db:
            current = db.execute("SELECT current_revision FROM drawings WHERE id=?", (drawing_id,)).fetchone()[0]
            expected = payload.get("expected_revision")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row["status"] == "cancelling":
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (now(), job["id"]))
                return None
            if current != expected:
                db.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?", ("版本冲突，候选修改未发布。", now(), job["id"]))
                return None
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM revisions WHERE drawing_id=?", (drawing_id,)).fetchone()[0]
            new_id = f"r{number:04d}"
            target = root / "revisions" / new_id
            target.parent.mkdir(parents=True, exist_ok=True)
            # A crash between filesystem rename and commit can leave an unreferenced directory.
            if target.exists():
                shutil.rmtree(target)
            summary = {"import": "导入原始图纸", "restore": "恢复历史图纸"}[job["kind"]]
            manifest = {"revision_id": new_id, "parent_revision": current, "job_id": job["id"], "created_at": now(), "summary": summary}
            (output / "manifest.json").write_text(encode(manifest), encoding="utf-8")
            (output / "changes.json").write_text(encode(metadata.get("changes", {})), encoding="utf-8")
            output.rename(target)
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", (new_id, drawing_id, number, now(), summary, encode(metadata)))
            db.execute("UPDATE drawings SET current_revision=?,status='ready' WHERE id=?", (new_id, drawing_id))
            committed_result = {"revision_id": new_id, "metadata": metadata, "changes": metadata.get("changes", {})}
            db.execute("UPDATE jobs SET status='succeeded',result=?,updated_at=? WHERE id=?", (encode(committed_result), now(), job["id"]))
        self.bus.publish({"type": "drawing.updated", "drawing_id": drawing_id, "revision_id": new_id})
        return {"revision_id": new_id, "metadata": metadata, "changes": metadata.get("changes", {})}
