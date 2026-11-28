"""公共文档只持久化脱敏文本，原件与解析产物限于本次临时目录。"""

import logging
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from fastapi import UploadFile

from knowledge.processor.import_processor.main_graph import import_app
from knowledge.review.privacy import redact_text
from knowledge.strategy.privacy import safe_filename, sanitize_markdown
from knowledge.utils.task_util import (
    update_task_status, add_done_task, set_task_result, get_task_result,
)

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class PublicImportInputError(ValueError):
    pass


class FileProcessService:
    def process_upload_file(self, file: UploadFile):
        filename = safe_filename(file.filename or "文档.md")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".md", ".markdown", ".txt"}:
            raise PublicImportInputError("仅支持 PDF、Markdown 和 UTF-8 文字文件")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise PublicImportInputError("文件为空或超过 50 MB")
        if suffix == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise PublicImportInputError("PDF 文件头无效")
        else:
            try:
                text = sanitize_markdown(data.decode("utf-8-sig"))
            except UnicodeError:
                raise PublicImportInputError("文字文件必须使用 UTF-8 编码") from None
            if not text.strip():
                raise PublicImportInputError("文档正文不能为空")
            data, suffix = text.encode("utf-8"), ".md"
        task_id = uuid4().hex
        directory = Path(tempfile.mkdtemp(prefix=f"shopkeeper-public-{task_id}-"))
        path = directory / ("input" + suffix)
        try:
            path.write_bytes(data)
            set_task_result(task_id, "document_title", redact_text(Path(filename).stem) or "公共文档")
            update_task_status(task_id, "processing")
            add_done_task(task_id, "upload_file")
        except Exception:
            self._cleanup(directory, task_id)
            raise RuntimeError("公共文档暂存失败，请稍后重试") from None
        return str(path), str(directory), task_id

    @staticmethod
    def _cleanup(directory, task_id):
        target = Path(directory).resolve()
        if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith(f"shopkeeper-public-{task_id}-"):
            raise ValueError("公共文档临时目录校验失败")
        shutil.rmtree(target)

    def run_main_graph(self, import_file_path, file_dir, task_id):
        status, error = "failed", ""
        state = {
            "task_id": task_id, "import_file_path": import_file_path, "file_dir": file_dir,
            "owner_id": "public", "document_title": get_task_result(task_id, "document_title", "公共文档"),
        }
        try:
            result = import_app.invoke(state)
            if not result.get("chunks"):
                raise ValueError("没有可索引的文本")
            status = "completed"
        except Exception as exc:
            error = "公共文档导入失败，请检查解析、向量模型和存储后重试"
            logger.warning("公共文档导入失败 (%s)", type(exc).__name__)
        finally:
            try:
                self._cleanup(file_dir, task_id)
            except (OSError, ValueError):
                status, error = "failed", "本次临时文件清理失败，需检查任务目录后重试"
                logger.warning("公共文档临时文件清理失败")
            set_task_result(task_id, "error", error)
            update_task_status(task_id, status)
