"""MinerU PDF 解析；原始解析产物仅写入调用方的任务目录。"""

import time
import zipfile
from pathlib import Path

import requests

from knowledge.processor.import_processor.base import BaseNode
from knowledge.processor.import_processor.exceptions import ValidationError


class PdfToMdNode(BaseNode):
    name = "pdf_to_md_node"

    def process(self, state):
        pdf_path = Path(state["pdf_path"])
        zip_url = self._upload_pdf_and_query_result(self.config, pdf_path)
        state["md_path"] = self._extract_md(zip_url, Path(state["file_dir"]), pdf_path)
        return state

    @staticmethod
    def _request(method, url, **kwargs):
        try:
            response = requests.request(method, url, timeout=(10, 60), **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            # 响应正文和签名 URL 可能包含凭据或文件内容，不写日志。
            raise RuntimeError("PDF 解析服务网络请求失败") from None

    @staticmethod
    def _json(response):
        try:
            result = response.json()
            if result.get("code") != 0:
                raise ValueError
            return result["data"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError("PDF 解析服务返回无效结果") from None

    def _upload_pdf_and_query_result(self, config, pdf_path_obj):
        if not config.mineru_base_url or not config.mineru_api_token:
            raise ValidationError(node_name=self.name, message="MinerU 配置不完整")
        base = config.mineru_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {config.mineru_api_token}"}
        data = self._json(self._request(
            "POST", base + "/file-urls/batch", headers=headers,
            json={"files": [{"name": pdf_path_obj.name, "data_id": "document"}], "model_version": "vlm"},
        ))
        with pdf_path_obj.open("rb") as source:
            self._request("PUT", data["file_urls"][0], data=source)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = self._json(self._request(
                "GET", base + "/extract-results/batch/" + data["batch_id"], headers=headers,
            ))["extract_result"][0]
            if result["state"] == "done":
                return result["full_zip_url"]
            if result["state"] == "failed":
                raise RuntimeError("PDF 解析失败")
            time.sleep(3)
        raise RuntimeError("PDF 解析超时")

    def _extract_md(self, zip_url, file_dir_obj, pdf_path_obj):
        zip_path = file_dir_obj / f"{pdf_path_obj.stem}_result.zip"
        response = self._request("GET", zip_url, stream=True)
        try:
            size = 0
            with zip_path.open("wb") as target:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 200 * 1024 * 1024:
                        raise RuntimeError("PDF 解析压缩包过大")
                    target.write(chunk)
        finally:
            response.close()
        extract_path = file_dir_obj / pdf_path_obj.stem
        extract_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 500 * 1024 * 1024:
                raise RuntimeError("PDF 解析产物过大")
            for info in archive.infolist():
                path = extract_path / info.filename
                if ("\\" in info.filename or not path.resolve().is_relative_to(extract_path.resolve())
                        or (info.external_attr >> 16) & 0o170000 == 0o120000):
                    raise RuntimeError("PDF 解析产物路径无效")
            archive.extractall(extract_path)
        target = extract_path / "full.md"
        if not target.is_file():
            raise RuntimeError("PDF 解析未生成 Markdown")
        return str(target.rename(extract_path / f"{pdf_path_obj.stem}.md"))
