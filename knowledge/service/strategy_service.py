"""个人策略任务生命周期：私有临时目录、持久状态、失败清理。"""

import logging
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from knowledge.review.privacy import redact_text, redact_value
from knowledge.strategy.privacy import safe_filename, sanitize_markdown
from knowledge.strategy.scope import IsolationError
from knowledge.utils.task_util import (
    update_task_status, get_task_info, add_done_task, set_task_result,
)

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class StrategyInputError(ValueError):
    pass


class StrategyService:
    def __init__(self, repository, vectors, graph=None, owner_id="default_user"):
        self.repository, self.vectors, self.graph = repository, vectors, graph
        self.owner_id = owner_id

    def stage(self, data, filename, title=None):
        filename = safe_filename(filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".md", ".markdown", ".txt"}:
            raise StrategyInputError("仅支持 PDF、Markdown 和文字")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise StrategyInputError("文件为空或超过 50 MB")
        if suffix == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise StrategyInputError("PDF 文件头无效")
        else:
            try:
                content = sanitize_markdown(data.decode("utf-8-sig"))
            except UnicodeError:
                raise StrategyInputError("文字文件必须使用 UTF-8 编码") from None
            if not content.strip():
                raise StrategyInputError("策略正文不能为空")
            data, suffix = content.encode("utf-8"), ".md"
        title = redact_text((title or Path(filename).stem).strip())[:200] or "个人策略"
        document_id = uuid4().hex
        directory = tempfile.mkdtemp(prefix="shopkeeper-strategy-")
        path = Path(directory) / ("input" + suffix)
        try:
            path.write_bytes(data)
            self.repository.create(document_id, self.owner_id, title, filename)
        except Exception:
            shutil.rmtree(directory)
            raise
        # 旧 /status 不能查询个人任务；个人接口先查持久化 owner 再读进度。
        set_task_result(document_id, "private", "personal")
        update_task_status(document_id, "processing")
        add_done_task(document_id, "upload_file")
        return {
            "task_id": document_id, "document_id": document_id, "owner_id": self.owner_id,
            "import_file_path": str(path), "file_dir": directory, "strategy_title": title,
        }

    def run(self, state):
        task_id = state["task_id"]
        status, error, count = "failed", None, 0
        insert_started = False
        try:
            self.vectors.prepare()
            graph = self.graph
            if graph is None:
                from knowledge.strategy.pipeline import build_strategy_graph
                graph = build_strategy_graph(self.vectors)
            # 标记后续失败可能留下向量；仅按本用户/本任务清理。
            insert_started = True
            result = graph.invoke(state)
            count = result.get("chunk_count", 0)
            if not count:
                raise RuntimeError("未写入策略切片")
            status = "completed"
        except Exception as exc:
            error = (str(exc) if isinstance(exc, IsolationError)
                     else "策略导入失败，请检查解析、向量模型和存储配置后重试")
            logger.warning("策略导入失败 (%s)", type(exc).__name__)
            if insert_started:
                try:
                    self.vectors.delete_document(self.owner_id, task_id)
                except Exception:
                    error += "；本次切片清理失败，需管理员检查"
        finally:
            try:
                shutil.rmtree(state["file_dir"])
            except OSError:
                status, error = "failed", "策略临时文件清理失败，需管理员检查"
                if insert_started:
                    try:
                        self.vectors.delete_document(self.owner_id, task_id)
                        count = 0
                    except Exception:
                        error += "；本次切片清理失败，需管理员检查"
            update_task_status(task_id, status)
            set_task_result(task_id, "error", error or "")
            try:
                self.repository.finish(task_id, self.owner_id, status, error, count)
            except Exception:
                update_task_status(task_id, "failed")
                set_task_result(task_id, "error", "导入状态写入失败，请稍后核对")
                logger.warning("策略状态写入失败")
                if status == "completed":
                    try:
                        self.vectors.delete_document(self.owner_id, task_id)
                    except Exception:
                        set_task_result(task_id, "error", "导入状态及切片清理失败，需管理员检查")

    def get(self, document_id):
        document = self.repository.get(document_id, self.owner_id)
        if document is None:
            return None
        return self._present(document)

    def recent(self):
        return [self._present(document) for document in self.repository.recent(self.owner_id)]

    def _present(self, document):
        document = dict(document)
        task_id = document.pop("_id")
        progress = get_task_info(task_id)
        # 同进程状态写入失败不能继续显示 processing；重启后使用数据库状态。
        if progress["status"] == "failed":
            from knowledge.utils.task_util import get_task_result
            document.update(status="failed", error=get_task_result(task_id, "error"))
        progress.pop("status")
        return redact_value({**document, **progress, "task_id": task_id})
