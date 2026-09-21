from backend.app.config import Settings
from backend.app.events import EventBus
from backend.app.jobs import JobManager
from backend.app.store import Store, now


async def test_cancelled_queued_import_does_not_leave_drawing_importing(tmp_path):
    settings = Settings(_env_file=None, cad_data_dir=tmp_path)
    store = Store(tmp_path / "app.sqlite")
    store.initialize()
    store.execute("INSERT INTO drawings VALUES (?,?,?,?,?,?,?)", ("drawing", "default", "test.dxf", ".dxf", None, "importing", now()))
    manager = JobManager(settings, store, EventBus())
    job = manager.submit("drawing", "import", {})
    cancelled = manager.cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    assert store.one("SELECT status FROM drawings WHERE id='drawing'")["status"] == "failed"
    # Starting the worker after cancellation must not publish a version.
    manager.start()
    await manager.queue.join()
    assert store.one("SELECT current_revision FROM drawings WHERE id='drawing'")["current_revision"] is None
    assert store.all("SELECT * FROM revisions") == []
    await manager.close()
