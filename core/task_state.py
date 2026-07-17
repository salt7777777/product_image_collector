import json
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

from core.models import ProductData


class TaskStateManager:
    """
    任务状态管理器。

    当前阶段用途：
        自动记录批量下载任务状态，为后续“断点续跑”做准备。

    状态文件示例：
        output/任务状态/task_state_20260717_170000.json
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_STOPPED = "stopped"
    STATUS_FINISHED = "finished"

    def __init__(
        self,
        output_dir: str | Path = "output",
        task_id: str | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.state_dir = self.output_dir / "任务状态"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.task_id = task_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.state_path = self.state_dir / f"task_state_{self.task_id}.json"

        self.state = {
            "task_id": self.task_id,
            "status": self.STATUS_PENDING,
            "created_at": self._now(),
            "updated_at": self._now(),
            "total": 0,
            "completed_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "running_product_key": "",
            "products": [],
        }

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def start(self, products: list[ProductData]):
        """
        初始化任务状态。
        """
        product_items = []

        for index, product in enumerate(products or [], start=1):
            product_items.append(
                {
                    "index": index,
                    "key": self.get_product_key(product),
                    "platform": product.platform,
                    "product_id": product.product_id,
                    "title": product.title,
                    "url": product.url,
                    "status": self.STATUS_PENDING,
                    "product_dir": "",
                    "started_at": "",
                    "finished_at": "",
                    "error": "",
                }
            )

        self.state["status"] = self.STATUS_RUNNING
        self.state["total"] = len(product_items)
        self.state["products"] = product_items
        self._refresh_counts()
        self.save()

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def mark_running(self, product: ProductData):
        key = self.get_product_key(product)
        item = self._find_product_item(key)

        if not item:
            return

        item["status"] = self.STATUS_RUNNING
        item["started_at"] = item.get("started_at") or self._now()
        item["error"] = ""

        self.state["status"] = self.STATUS_RUNNING
        self.state["running_product_key"] = key

        self._refresh_counts()
        self.save()

    def mark_done(
        self,
        product: ProductData,
        product_dir: str | Path | None = None,
    ):
        key = self.get_product_key(product)
        item = self._find_product_item(key)

        if not item:
            return

        item["status"] = self.STATUS_DONE
        item["finished_at"] = self._now()
        item["error"] = ""

        if product_dir:
            item["product_dir"] = str(Path(product_dir).resolve())

        if self.state.get("running_product_key") == key:
            self.state["running_product_key"] = ""

        self._refresh_counts()
        self.save()

    def mark_failed(
        self,
        product: ProductData,
        error: str = "",
        product_dir: str | Path | None = None,
    ):
        key = self.get_product_key(product)
        item = self._find_product_item(key)

        if not item:
            return

        item["status"] = self.STATUS_FAILED
        item["finished_at"] = self._now()
        item["error"] = error or ""

        if product_dir:
            item["product_dir"] = str(Path(product_dir).resolve())

        if self.state.get("running_product_key") == key:
            self.state["running_product_key"] = ""

        self._refresh_counts()
        self.save()

    def mark_stopped(self):
        """
        标记任务被用户停止。

        当前正在 running 的商品会改成 pending，
        方便后续续跑时重新处理。
        """
        for item in self.state.get("products", []):
            if item.get("status") == self.STATUS_RUNNING:
                item["status"] = self.STATUS_PENDING
                item["error"] = "任务中途停止，等待续跑"

        self.state["status"] = self.STATUS_STOPPED
        self.state["running_product_key"] = ""
        self._refresh_counts()
        self.save()

    def mark_finished(self):
        self.state["status"] = self.STATUS_FINISHED
        self.state["running_product_key"] = ""
        self._refresh_counts()
        self.save()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_pending_products(self) -> list[dict]:
        return [
            item for item in self.state.get("products", [])
            if item.get("status") in [self.STATUS_PENDING, self.STATUS_FAILED]
        ]

    def get_state_path(self) -> Path:
        return self.state_path

    # ------------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------------

    def save(self):
        self.state["updated_at"] = self._now()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, state_path: str | Path):
        state_path = Path(state_path)
        data = json.loads(state_path.read_text(encoding="utf-8"))

        output_dir = state_path.parent.parent
        manager = cls(
            output_dir=output_dir,
            task_id=data.get("task_id"),
        )
        manager.state_path = state_path
        manager.state = data
        return manager

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def get_product_key(product: ProductData) -> str:
        platform = product.platform or "unknown"
        product_id = product.product_id or ""

        if product_id:
            return f"{platform}_{product_id}"

        return f"{platform}_{abs(hash(product.url or ''))}"

    def _find_product_item(self, key: str):
        for item in self.state.get("products", []):
            if item.get("key") == key:
                return item
        return None

    def _refresh_counts(self):
        products = self.state.get("products", [])

        completed = sum(1 for item in products if item.get("status") == self.STATUS_DONE)
        failed = sum(1 for item in products if item.get("status") == self.STATUS_FAILED)
        pending = sum(1 for item in products if item.get("status") == self.STATUS_PENDING)

        self.state["completed_count"] = completed
        self.state["failed_count"] = failed
        self.state["pending_count"] = pending

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
