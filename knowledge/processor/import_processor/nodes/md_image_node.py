import base64
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging import Logger
from pathlib import Path

from openai import OpenAI

from knowledge.processor.import_processor.base import BaseNode, T
from knowledge.processor.import_processor.config import ImportConfig
from knowledge.processor.import_processor.exceptions import StateFieldError, FileProcessingError
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.utils.clients.ai_clients import AIClients
from knowledge.utils.clients.storage_clients import StorageClients

# Markdown 标题行:1~6级标题,# 后必须有空格
HEADING_PATTERN = re.compile(r'^#{1,6}\s+')

# 任意 Markdown 图片引用(不区分文件名)
IMG_ANY_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# 三个反引号,等价于 "```"
BACKTICKS = chr(96) * 3


class ImageContext:
    """图片上下文:上标题名称、上文、下文"""
    heading_name: str
    above_context: str
    below_context: str

    def __init__(self, heading_name: str, above_context: str, below_context: str):
        self.heading_name = heading_name
        self.above_context = above_context
        self.below_context = below_context


class ImageInfo:
    image_name: str
    image_path: Path
    context: ImageContext

    def __init__(self, image_name: str, image_path: Path, context: ImageContext):
        self.image_name = image_name
        self.image_path = image_path
        self.context = context


class _MdFileHandler:
    def __init__(self, logger: Logger, node_name: str):
        self.logger = logger
        self.node_name = node_name

    def read_md(self, md_path: str) -> tuple[str, Path, Path]:
        # 1. 校验md_path是否为空
        if not md_path:
            self.logger.error("md_path is empty")
            raise StateFieldError(node_name=self.node_name, field_name="md_path", expected_type=str,
                                  message="md_path is empty")
        # 2. 校验md_path是否存在
        md_path_obj = Path(md_path)
        if not md_path_obj.exists():
            self.logger.error(f"md_path {md_path} does not exist")
            raise FileProcessingError(node_name=self.node_name,
                                      message=f"md_path {md_path} does not exist")
        # 3. 读取md文件的内容到内存
        try:
            with open(md_path_obj, "r", encoding="utf-8") as f:
                md_content = f.read()
        except Exception as e:
            self.logger.error(f"read md file {md_path} failed, error: {str(e)}")
            raise FileProcessingError(node_name=self.node_name,
                                      message=f"read md file {md_path} failed, error: {str(e)}")
        # 4. 获取md文件的图片目录路径
        image_dir_obj = md_path_obj.parent / "images"

        # 5. 返回md_content,md_path_obj,image_dir_obj
        return md_content, md_path_obj, image_dir_obj

    def backup_md(self, md_content: str, md_path: str) -> None:
        """把处理后的md内容备份成新md文件(便于测试观察)"""
        # 1. 构建备份文件的路径: 在原文件目录下,名字为 原文件名_new.原文件后缀
        md_path_obj = Path(md_path)
        backup_md_path_obj = md_path_obj.parent / (md_path_obj.stem + "_new" + md_path_obj.suffix)
        # 2. 将md_content写入到备份文件中
        try:
            with open(backup_md_path_obj, "w", encoding="utf-8") as f:
                f.write(md_content)
        except Exception as e:
            # 备份失败只告警,不影响主流程
            self.logger.warning(f"备份md文件失败: {e},但是不影响程序的运行")
            return
        self.logger.info(f"文件备份成功，文件路径是{str(backup_md_path_obj)}")


class _ImageScanner:
    def __init__(self, logger: Logger, config: ImportConfig):
        self.logger = logger
        self.config = config

    def scan_img_dir(self, image_dir_obj: Path, md_content: str) -> tuple[list[ImageInfo], list[str]]:
        # 把md切成行,便于按行定位(后面替换内容也基于这些行)
        md_lines = md_content.split("\n")
        # 收集匹配结果
        image_info_list = []
        for image_file in image_dir_obj.iterdir():
            # 2.1 过滤子目录
            if not image_file.is_file():
                continue
            # 2.2 过滤非图片文件
            if image_file.suffix.lower() not in self.config.image_extensions:
                continue
            # 2.3.1 构建该图片专属的正则
            img_ref_pattern = re.compile(
                r"!\[[^\]]*\]\([^)]*" + re.escape(image_file.name) + r"[^)]*\)\s*$",
                re.IGNORECASE,
            )

            # 2.3.2 逐行扫描,跳过代码块内的伪图片引用
            matched = False
            in_code_block = False
            for line_no, md_line in enumerate(md_lines):
                stripped = md_line.strip()
                # 遇到代码块标记行,切换状态
                if stripped.startswith(BACKTICKS):
                    in_code_block = not in_code_block
                    continue
                # 代码块内的一律不匹配
                if in_code_block:
                    continue
                if not img_ref_pattern.search(md_line):
                    continue
                # 2.3.3 真的匹配到了:记录行号与上下文对象
                matched = True
                image_context = self.extract_context(
                    md_lines=md_lines, line_no=line_no,
                    max_length=self.config.img_content_length
                )

                image_info = ImageInfo(
                    image_name=image_file.name,
                    image_path=image_file,
                    context=image_context
                )
                image_info_list.append(image_info)

            # 2.3.4 一张图都没匹配到:记日志并跳过,不中断流程
            if not matched:
                self.logger.warning(f"【匹配图片】md中未找到引用,跳过: {image_file.name}")
        return image_info_list, md_lines

    def _collect_paragraphs(self, lines: list, max_length: int, is_above: bool) -> str:
        """按空行/其它图片行把 lines 切分为段落,并在长度阈值内收集。

        - 遇到空行或其它图片行 -> 当前段结束,收集并清空
        - 其余行 -> 加入当前段
        - 上文(is_above=True):先反转段落列表,使离图片最近的段落优先被收集;
          收集完再反转回来,保证返回给VLM的顺序是从前到后
        """
        # 1. 切分段落
        paragraphs = []
        current = []
        for line in lines:
            stripped = line.strip()
            if stripped == "" or IMG_ANY_PATTERN.search(line):
                if current:
                    paragraphs.append(current)
                    current = []
            else:
                current.append(line)
        if current:
            paragraphs.append(current)

        # 2. 上文:反转段落列表(靠近图片的段落排前面,截断时优先保留)
        if is_above:
            paragraphs.reverse()

        # 3. 按长度阈值收集段落:累计超过 max_length 就停止
        collected = []
        total_length = 0
        for para in paragraphs:
            para_length = len("\n".join(para))
            if total_length + para_length > max_length:
                break
            collected.append(para)
            total_length += para_length

        # 4. 上文:把收集到的段落再反转回正常阅读顺序
        if is_above:
            collected.reverse()

        # 5. 段落之间用两个换行拼接
        return "\n\n".join("\n".join(para) for para in collected)

    def extract_context(self, md_lines: list, line_no: int, max_length: int) -> ImageContext:
        """提取图片上下文,返回 ImageContext(上标题名称、上文、下文)。"""
        # 1. 找图片上方最近的标题(跳过代码块内的 # 文本),并记录标题行索引
        heading_name = ""
        prev_index = -1
        in_code_block = False
        for i in range(line_no - 1, -1, -1):
            stripped = md_lines[i].strip()
            if stripped.startswith(BACKTICKS):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            if HEADING_PATTERN.match(md_lines[i]):
                heading_name = stripped.lstrip("#").strip()
                prev_index = i
                break

        # 2. 找图片下方最近的标题行索引(找不到则默认到文档末尾)
        next_index = len(md_lines)
        in_code_block = False
        for i in range(line_no + 1, len(md_lines)):
            stripped = md_lines[i].strip()
            if stripped.startswith(BACKTICKS):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            if HEADING_PATTERN.match(md_lines[i]):
                next_index = i
                break

        # 3. 上文:上标题行(不含) ~ 图片行(不含)
        above_context = self._collect_paragraphs(
            md_lines[prev_index + 1:line_no], max_length, is_above=True
        )
        # 4. 下文:图片行(不含) ~ 下标题行(不含)
        below_context = self._collect_paragraphs(
            md_lines[line_no + 1:next_index], max_length, is_above=False
        )

        return ImageContext(
            heading_name=heading_name,
            above_context=above_context,
            below_context=below_context,
        )


class _VLMSummarizer:
    """图片摘要生成器:并发调用VLM生成图片摘要"""

    def __init__(self, logger: Logger, config: ImportConfig):
        self.logger = logger
        self.config = config
        # 限速锁:保证并发worker之间的调用间隔控制是线程安全的
        self._limit_lock = threading.Lock()

    def _rate_limit(self, last_call_time: list, interval: float) -> None:
        """简单滑动间隔限速:保证两次VLM调用之间至少间隔interval秒,防止超出每分钟请求配额"""
        with self._limit_lock:
            now = time.time()
            wait = last_call_time[0] + interval - now
            if wait > 0:
                time.sleep(wait)
            last_call_time[0] = time.time()

    def summarize_all(self, image_info_list: list[ImageInfo], document_name: str) -> dict:
        # 存储每张图片的摘要信息，key:图片名字，value:图片摘要信息
        image_summaries = {}
        if not image_info_list:
            return image_summaries

        # 1. 创建VLM模型对象(单例客户端,线程安全)
        try:
            vlm_client = AIClients.get_vlm_client()
        except ConnectionError as e:
            # 兜底:如果连接vlm模型失败,则给每张图片设置描述信息为"暂无摘要信息"
            self.logger.warning(f"无法连接到VLM模型，图片描述信息将无法生成: {e}")
            for image_info in image_info_list:
                image_summaries[image_info.image_name] = "暂无摘要信息"
            return image_summaries

        # 2. 并发调用VLM,给每一张图片生成摘要信息
        # 并发数:不超过配置的每分钟请求数,同时限制最大线程数
        max_workers = max(1, min(self.config.requests_per_minute, 6))
        # 两次调用之间的最小间隔(秒):按每分钟请求数换算
        call_interval = max(1.0, 60.0 / self.config.requests_per_minute) if self.config.requests_per_minute > 0 else 1.0
        last_call_time = [0.0]  # 可变对象,供各worker共享

        def _worker(image_info: ImageInfo) -> tuple[str, str]:
            # 每次调用前限速,防止并发下超出每分钟请求数
            self._rate_limit(last_call_time, call_interval)
            summary = self._summarize_single(vlm_client, image_info, document_name)
            return image_info.image_name, summary

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vlm") as executor:
            futures = {executor.submit(_worker, img): img for img in image_info_list}
            for future in as_completed(futures):
                image_info = futures[future]
                try:
                    name, summary = future.result()
                except Exception as e:
                    # worker内异常兜底:单张图片失败不中断整体,该图无摘要
                    self.logger.warning(f"处理图片{image_info.image_name}摘要的线程异常: {e}")
                    continue
                image_summaries[name] = summary

        return image_summaries

    def _summarize_single(self, vlm_client: OpenAI, image_info: ImageInfo, document_name: str) -> str:
        # 1. 拼接上下文
        final_context = "\n".join([
            image_info.context.heading_name,
            image_info.context.above_context,
            image_info.context.below_context
        ])

        # 2. 读取图片
        try:
            with open(image_info.image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
        except IOError as e:
            self.logger.warning(f"读取图片文件失败，无法提取图片摘要: {e}")
            return "暂无摘要"
        # 3. 组装给大模型的输入信息
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"任务：为Markdown文档中的图片生成一个简短的中文标题。\n"
                            f"背景信息：\n"
                            f"  1. 所属文档标题：\"{document_name}\"\n"
                            f"  2. 图片上下文：{final_context}\n"
                            f"请结合图片内容和上述上下文信息，"
                            f"用中文简要总结这张图片的内容，"
                            f"生成一个精准的中文标题摘要（不要包含图片二字）。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}"
                        },
                    },
                ]
            }
        ]
        # 4. 调用大模型
        try:
            vlm_result = vlm_client.chat.completions.create(
                model=self.config.vl_model,
                messages=messages
            )
        except Exception as e:
            self.logger.warning(f"调用VLM模型失败，无法提取图片{image_info.image_name}的摘要: {e}")
            return "暂无摘要"
        # 5. 提取摘要信息
        return vlm_result.choices[0].message.content.strip()


class ImageUploader:
    """图片上传器:上传图片到MinIO,并把(图片摘要+远程URL)回填到md内容中"""

    def __init__(self, logger: Logger, config: ImportConfig):
        self.logger = logger
        self.config = config

    def upload_and_replace(self, md_lines: list, document_name: str,
                            image_info_list: list, image_summaries: dict) -> str:
        """上传所有图片到MinIO,并替换md中的图片语法,返回替换后的md内容"""
        # 1. 上传图片,得到(图片名:MinIO远程URL)
        remote_urls = self.upload_all(document_name, image_info_list)
        # 2. 替换md中的内容,返回替换之后的md的内容
        new_md_content = self._replace_md_content(md_lines, image_summaries, remote_urls)
        return new_md_content

    def upload_all(self, document_name: str, image_info_list: list) -> dict:
        # 存放每张图片在MinIO服务器上的访问路径
        remote_urls = {}
        # 1. 获取MinIO客户端对象
        try:
            minio_client = StorageClients.get_minio()
        except ConnectionError as e:
            # 兜底:连不上MinIO,所有图片使用原路径
            self.logger.warning(f"无法连接到MinIO服务器，图片将无法上传: {e}")
            for image_info in image_info_list:
                remote_urls[image_info.image_name] = str(image_info.image_path)
            return remote_urls

        # 2. 备好URL前缀和桶名
        base_url = self.config.get_minio_base_url()
        bucket_name = self.config.minio_bucket

        # 3. 遍历每一张图片,上传到MinIO
        for image_info in image_info_list:
            # 3.1 构建object_name: 图片存储在MinIO上的路径 文档名/图片名
            object_name = document_name + "/" + image_info.image_name
            try:
                minio_client.fput_object(
                    bucket_name=bucket_name,
                    object_name=object_name,
                    file_path=str(image_info.image_path)
                )
            except Exception as e:
                # 兜底:单张上传失败使用图片的原路径,不影响其它图片
                self.logger.warning(f"图片{image_info.image_name}上传MinIO失败,保留原路径: {e}")
                remote_urls[image_info.image_name] = str(image_info.image_path)
                continue

            # 3.2 组装文件在MinIO上的访问url
            remote_url = base_url + "/" + bucket_name + "/" + object_name
            remote_urls[image_info.image_name] = remote_url
            self.logger.info(f"图片{image_info.image_name}上传成功,URL: {remote_url}")

        self.logger.info(f"所有图片处理完成,共{len(remote_urls)}张")
        return remote_urls

    def _replace_md_content(self, md_lines: list, image_summaries: dict, remote_urls: dict) -> str:
        """把md中图片行替换为 ![图片摘要](MinIO远程URL) """
        # 匹配md中图片语法的正则:第一个匹配组是alt文本,第二个匹配组是图片路径
        pattern = re.compile(r"!\[(.*?)\]\((.*?)\)")
        # 匹配代码块标记行的正则
        code_fence_pattern = re.compile(r"^\s*```")

        in_code_fence = False
        # 存储替换后的md行
        new_md_lines = []
        for line in md_lines:
            # 遇到代码块标记行,切换状态
            if code_fence_pattern.match(line):
                in_code_fence = not in_code_fence

            match = pattern.match(line)
            # 1. 该行是图片且不在代码块里,才进行替换
            if not in_code_fence and match:
                # 通过图片路径取图片名,与已处理的图片进行匹配
                image_name = Path(match.group(2)).name
                image_summary = image_summaries.get(image_name)
                remote_url = remote_urls.get(image_name)
                # 未处理过的图片(如md引用了但不在images目录中)保持原样
                if image_summary is None or remote_url is None:
                    new_md_lines.append(line)
                else:
                    new_md_lines.append(f"![{image_summary}]({remote_url})")
            else:
                # 不需要替换
                new_md_lines.append(line)

        return "\n".join(new_md_lines)


class MdImageNode(BaseNode):
    name = "md_image_node"

    def __init__(self):
        super().__init__()
        self.md_file_handler = _MdFileHandler(logger=self.logger, node_name=self.name)
        self.image_scanner = _ImageScanner(logger=self.logger, config=self.config)
        self.vlm_summarizer = _VLMSummarizer(logger=self.logger, config=self.config)
        self.image_uploader = ImageUploader(logger=self.logger, config=self.config)

    def process(self, state: ImportGraphState) -> ImportGraphState:
        # 1. 读取md文件的内容到内存
        md_path = state.get("md_path")
        md_content, md_path_obj, image_dir_obj = self.md_file_handler.read_md(md_path)

        # 1.1 图片目录不存在,无需处理图片,直接更新state返回
        if not image_dir_obj.exists():
            self.logger.warning(f"文件{md_path_obj.name}暂无图片需要处理")
            state["md_content"] = md_content
            return state

        # 2. 遍历图片目录中的每一个图片文件,与md中的引用进行匹配并提取上下文
        image_info_list, md_lines = self.image_scanner.scan_img_dir(image_dir_obj, md_content)

        # 3. 根据图片的上下文,使用VLM模型识别图片、获取图片的描述信息
        image_summaries = self.vlm_summarizer.summarize_all(
            image_info_list=image_info_list, document_name=md_path_obj.stem
        )

        # 4. 将图片上传到MinIO服务器,将图片的描述信息、MinIO的URL回填到md文档中
        new_md_content = self.image_uploader.upload_and_replace(
            md_lines=md_lines, document_name=md_path_obj.stem,
            image_info_list=image_info_list, image_summaries=image_summaries
        )
        # print(new_md_content)
        # 5. 将回填后的内容，备份成新md文件（便于测试观察）
        self.md_file_handler.backup_md(new_md_content, md_path)

        # # 6. 更新state并返回
        state["md_content"] = new_md_content
        return state


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    state = {
        "md_path": r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\output_dir\万用表RS-12的使用\万用表RS-12的使用.md",
    }
    md_image_node = MdImageNode()
    md_image_node(state)
