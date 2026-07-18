import json
from pathlib import Path
from datetime import datetime

from core.models import ProductData


class TaskStateManager:
    """
    任务状态管理器。

    当前用途：
        1. 自动记录批量下载任务状态；
        2. 为断点续跑提供状态文件；
        3. 支持查找最近一个未完成任务；
        4. 支持提取未完成商品链接；
        5. 支持将旧任务标记为 resumed，避免反复恢复同一个旧任务。
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_STOPPED = "stopped"
    STATUS_FINISHED = "finished"
    STATUS_RESUMED = "resumed"

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
        标记任务停止。

        当前正在 running 的商品改成 pending，
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

    def mark_resumed(self):
        """
        标记该任务已经被“继续上次任务”功能接管。

        作用：
            防止同一个 stopped 任务被反复识别出来。
        """
        self.state["status"] = self.STATUS_RESUMED
        self.state["running_product_key"] = ""
        self.state["resumed_at"] = self._now()
        self._refresh_counts()
        self.save()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_pending_products(self) -> list[dict]:
        """
        返回未完成商品记录。

        包含：
            pending
            failed
            running

        running 用于程序异常退出时恢复。
        """
        return [
            item for item in self.state.get("products", [])
            if item.get("status") in [
                self.STATUS_PENDING,
                self.STATUS_FAILED,
                self.STATUS_RUNNING,
            ]
        ]

    def get_unfinished_urls(self) -> list[str]:
        urls = []

        for item in self.get_pending_products():
            url = item.get("url", "")
            if url and url not in urls:
                urls.append(url)

        return urls

    def is_finished(self) -> bool:
        return self.state.get("status") == self.STATUS_FINISHED

    def is_resumed(self) -> bool:
        return self.state.get("status") == self.STATUS_RESUMED

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

    @classmethod
    def find_latest_unfinished(
        cls,
        output_dir: str | Path = "output",
    ) -> Path | None:
        """
        查找最近一个未完成任务状态文件。

        忽略：
            finished
            resumed

        未完成条件：
            1. 顶层 status 不是 finished/resumed；
            2. products 中仍有 pending / failed / running。
        """
        output_dir = Path(output_dir)
        state_dir = output_dir / "任务状态"

        if not state_dir.exists():
            return None

        files = sorted(
            state_dir.glob("task_state_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        for path in files:
            try:
                manager = cls.load(path)
                status = manager.state.get("status")

                # 已完成或已经被恢复接管的任务，不再作为未完成任务返回
                if status in [cls.STATUS_FINISHED, cls.STATUS_RESUMED]:
                    continue

                # 有待处理商品，认为可恢复
                if manager.get_pending_products():
                    return path

                # 状态异常但未 finished/resumed，也保留恢复机会
                if status not in [cls.STATUS_FINISHED, cls.STATUS_RESUMED]:
                    return path

            except Exception:
                continue

        return None

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
