import json
import logging
import re
from pathlib import Path
from typing import Tuple, List, Dict, Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from knowledge.processor.import_processor.base import BaseNode, T
from knowledge.processor.import_processor.exceptions import StateFieldError
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.utils.markdown_util import MarkdownTableLinearizer


class DocumentSplitNode(BaseNode):
    name = "document_split_node"
    # 匹配 _split_long_section 切分产生的 "_N" 后缀
    _SPLIT_SUFFIX = re.compile(r"_\d+$")
    # BGE-M3窗口8192 tokens,合并结果远超粒度目标也无风险,此门仅防未来调大min后失控,可按需删除
    MERGE_HARD_LIMIT = 6000

    def process(self, state: ImportGraphState) -> ImportGraphState:
        #1. 参数校验
        new_md_content,file_title,max_content_length,min_content_length = self._validate_state(state)
        #2. 按标题切分
        sections_by_head = self._split_by_head(new_md_content,file_title)
        #3. 二次切分或者合并
        final_sections = self._split_and_merge(sections_by_head,max_content_length,min_content_length)
        #4. 将切分完的内容，组装成后续节点可直接使用的chunks
        chunks = self._build_chunks(final_sections)
        # for chunk in chunks:
        #     print(chunk)
        #5. 将chunks备份成json文件，便于后续测试
        self._backup_chunks(chunks, state.get("file_dir", ""), file_title)

        #6. 更新state并返回
        state["chunks"] = chunks
        return state

    def _validate_state(self, state:ImportGraphState) -> Tuple[str, str,int,int]:
        #1. 统一换行符: 将md_content中的"\r\n"与"\r"统一替换成"\n"
        #1.1 从state中获取md_content
        md_content = state.get("md_content","")
        # 注意顺序: 必须先替换"\r\n"再替换"\r",否则"\r\n"会被先拆成"\n\n"多出空行
        new_md_content = md_content.replace("\r\n","\n").replace("\r","\n")
        #2. 校验配置信息: 一个切片的最大长度、一个切片的最小长度
        max_content_length = self.config.max_content_length
        min_content_length = self.config.min_content_length
        if not max_content_length or max_content_length <= 0 or not min_content_length or min_content_length <= 0 or max_content_length < min_content_length :
            self.logger.error(f"max_content_length={max_content_length} 或者 min_content_length={min_content_length}配置错误,请检查")
            raise StateFieldError(node_name=self.name,field_name="max_content_length or min_content_length",expected_type=int)
        #3. 获取文档标题(给后续步骤作为兜底使用)
        file_title = state.get("file_title")
        if not file_title:
            self.logger.error("file_title is empty")
            raise StateFieldError(node_name=self.name,field_name="file_title",expected_type=str,message="file_title is empty")
        #4. 将new_md_content, file_title, config.max_content_length, config.min_content_length给下一步使用
        return new_md_content,file_title,max_content_length,min_content_length

    def _split_by_head(self, md_content:str, file_title:str) -> List[Dict[str,Any]]:
        """
        我们最终得到的产物是切成的块列表，我们分每一块是什么结构:
        {
            "body":这块的正文内容,
            "title":这块的标题,
            "parent_title":这块的父标题,
            "file_title":这块所在的文档标题
        }
        """
        #1. md_content按行切分
        md_lines = md_content.split("\n")
        #定义匹配md标题的正则
        heading_re = re.compile(r"^\s*(#{1,6})\s+(.+)")
        # 匹配代码块
        code_fence_pattern = re.compile(r"^\s*```")
        in_code_fence = False
        #声明hierarchy列表用于存储各级标题,目的是为了识别父标题
        hierarchy = [""]*7
        #2. 遍历md_content的每一行，判断是否遇到了标题，但是注意要排除代码块的干扰
        #声明变量body_lines，用来存储当前标题块section的内容部分
        body_lines = []
        #声明变量current_title，表示当前标题
        current_title = ""
        #声明变量current_level，表示当前标题的层级
        current_level = 0
        # 声明变量final_sections,用来存储所有的块
        final_sections = []
        # 声明一个内部方法,用来将某个标题块进行收集
        def _flush():
            # 收集body的内容
            body = "\n".join(body_lines)
            # 收集title
            title = current_title

            #判断是否存在current_title、是否存在body，如果都不存在，则不收集该段内容
            if not body and not title:
                return

            # 收集parent_title,从当前标题往前遍历hierarchy数组
            parent_title = ""
            for i in range(current_level - 1,0,-1):
                if hierarchy[i] != "":
                    parent_title = hierarchy[i]
                    break

            section = {
                "body":body,
                "title":title if title else file_title, #使用file_title为title进行兜底
                "parent_title":parent_title if parent_title else current_title,
                "file_title":file_title
            }

            #将section添加到final_sections中
            final_sections.append(section)

        for index,md_line in enumerate(md_lines):
            #2.1 判断md_line是不是标题行，并且不在代码围栏中
            if code_fence_pattern.match(md_line):
                in_code_fence = not in_code_fence

            #匹配标题
            match = heading_re.match(md_line)
            if match and not in_code_fence:
                # 当前行是标题行，并且没有在代码块中
                # 收集上一个标题块的section
                _flush()
                # 设置当前标题的值
                current_title = md_line
                # 清空body_lines
                body_lines = []
                # 将当前标题添加到hierarchy数组中,并且要记录当前标题的层级
                # 获取当前标题的层级:就是#的个数，使用正则匹配的第一个捕获组能拿到#的个数
                current_level = len(match.group(1))
                # 将当前标题存储到hierarchy数组中的current_level
                hierarchy[current_level] = current_title
                # 清空hierarchy中current_level下标后的所有元素
                for i in range(current_level+1,7):
                    hierarchy[i] = ""
            else:
                # 当前行不是标题行，或者当前行在代码块中，将当前行的内容添加到当前section的body中
                body_lines.append(md_line)

        # 遍历到最后，遇不到title了，要将前一个title的所有内容收集起来
        _flush()

        return final_sections

    #二次切分与合并
    def _split_and_merge(self, sections:List[Dict[str,Any]], max_content_length:int, min_content_length:int) -> List[Dict[str,Any]]:
        #1. 声明current_sections用于收集二次切分后的section
        current_sections = []
        #2. 遍历每一个section,对超长的section进行二次切分
        for section in sections:
            split_sections = self._split_long_section(section,max_content_length)
            current_sections.extend(split_sections)


        #3. 针对小的section进行合并
        final_sections = self._merge_small_sections(current_sections,max_content_length,min_content_length)
        #4. 返回二次切分和合并后的section
        self.logger.info(f"文档切分完成: 一级切分{len(sections)}块,二次切分后{len(current_sections)}块,合并后{len(final_sections)}块")

        return final_sections

    def _merge_small_sections(self, sections:List[Dict[str,Any]], max_content_length:int, min_content_length:int) -> List[Dict[str,Any]]:
        """
        把长度不足min_content_length的小section,向后合并到相邻的section中

        合并规则:
        1. 只有"同源"的相邻块才允许合并(见_is_mergeable),不同源的小块宁可保留
        2. 同源碎片回并时还原基础标题; 父子合并时title沿用子块的标题
        3. 被吞块的title存入merged_titles留痕,保证检索关键词不丢失
        4. 合并结果受MERGE_HARD_LIMIT保险门约束(BGE-M3窗口8192 tokens,此门仅防调参失控)
        """
        if not sections:
            return []
        final_sections = []
        merged_count = 0
        index = 0
        while index < len(sections):
            current_section = sections[index]
            current_body = current_section.get("body","")
            merged_titles = []
            #1. 当前section太小,并且后面还有section,就尝试向后合并
            while len(current_body) < min_content_length and index + 1 < len(sections):
                next_section = sections[index + 1]
                #2. 语义准入: 不同源不合并
                if not self._is_mergeable(current_section, next_section):
                    break
                merged_body = current_body + "\n\n" + next_section.get("body","")
                #3. 窗口保险门
                if len(next_section["title"]) + 2 + len(merged_body) > self.MERGE_HARD_LIMIT:
                    break
                merged_titles.append(current_section["title"])
                current_base = self._SPLIT_SUFFIX.sub("", current_section["title"])
                next_base = self._SPLIT_SUFFIX.sub("", next_section["title"])
                #4. 同源碎片回并则还原基础标题, 否则(父子合并)归属子块标题
                new_title = current_base if current_base == next_base else next_section["title"]
                current_section = {
                    "body":merged_body,
                    "title":new_title,
                    "parent_title":next_section["parent_title"],
                    "file_title":next_section["file_title"],
                    "merged_titles":merged_titles,
                }
                current_body = merged_body
                merged_count += 1
                index += 1
            final_sections.append(current_section)
            index += 1
        if merged_count:
            self.logger.info(f"共合并了{merged_count}个过短的section")
        return final_sections

    def _is_mergeable(self, current:Dict[str,Any], next_section:Dict[str,Any]) -> bool:
        """只有同源的相邻section才允许合并,避免跨标题粘连"""
        # 跨文档绝不合并
        if current["file_title"] != next_section["file_title"]:
            return False
        current_base = self._SPLIT_SUFFIX.sub("", current["title"])
        next_base = self._SPLIT_SUFFIX.sub("", next_section["title"])
        has_split_suffix = self._SPLIT_SUFFIX.search(current["title"]) or self._SPLIT_SUFFIX.search(next_section["title"])
        # 1. 同源碎片: _split_long_section切出的"xxx_1/xxx_2",允许回并
        if has_split_suffix and current_base == next_base:
            return True
        # 2. 父子关系: next的父标题就是current的标题,节引言并入第一个子块
        if next_section["parent_title"] == current["title"]:
            return True
        return False

    def _split_long_section(self, section:Dict[str,Any], max_content_length:int) -> List[Dict[str,Any]]:
        """
        {
            "body":这块的正文内容,
            "title":这块的标题,
            "parent_title":这块的父标题,
            "file_title":这块所在的文档标题
        }
        """
        #1. 判断section的大小是否超过了max_content_length
        #1.1. 计算section的长度
        #1.1.1 获取section的title
        title = section.get("title")
        # 防御性编程:如果title太长了，我们只取title的前80个字符
        if len(title) > 80:
            title = title[:80]

        title_prefix = title + "\n\n"
        #1.1.2 获取body
        body = section.get("body")

        # 对body中的表格进行处理
        if "<table>" in body:
            body = MarkdownTableLinearizer.process(body)
            section["body"] = body

        #1.1.3 计算长度
        total_length = len(body) + len(title_prefix)

        #1.2 判断total_length是否大于max_content_length
        if total_length <= max_content_length:
            # 说明当前section的长度没有超过最大长度，不需要切分
            return [section]

        # 计算可切分的长度=最大长度-标题长度
        split_content_length=max_content_length-len(title_prefix)
        # 当前section的长度超过了最大长度,需要切分,使用RecursiveCharacterTextSplitter文本切分器进行切分
        text_splitter = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", "。", "？", "！", "；", ".", "?", "!", ';', " ", ""],
            chunk_size=split_content_length,
            keep_separator=True,
            chunk_overlap=0
        )

        # 使用text_splitter对body进行切分
        split_body_list = text_splitter.split_text(body)
        # 判断切分后的内容是否有多个字符串
        if len(split_body_list) == 1:
            # 说明其实没有对这个section进行切分
            return [section]
        # 如果切分成了多个字符串,那么我们需要遍历切分后的字符串列表，将每一个字符串组装成一个section
        result_sections = []
        for index,split_body in enumerate(split_body_list):
            split_section = {
                "body":split_body,
                "title":f"{title}_{index + 1}",
                "parent_title": section.get("parent_title"),
                "file_title": section.get("file_title")
            }

            result_sections.append(split_section)

        return result_sections

    def _build_chunks(self, final_sections:List[Dict[str,Any]]) -> List[Dict[str,Any]]:
        """
        把切分好的section组装成后续节点(向量化、商品名识别)可直接使用的chunks

        chunk结构:
        {
            "text": 向量化使用的完整文本(标题 + 正文),
            "title": 这一块的标题,
            "parent_title": 这一块的父标题,
            "file_title": 这一块所在的文档标题
        }
        """
        chunks = []
        for section in final_sections:
            chunk = {
                # 与_split_long_section中total_length的计算口径保持一致: 标题 + 两个换行 + 正文
                "text": section["title"] + "\n\n" + section["body"],
                "title": section["title"],
                "parent_title": section["parent_title"],
                "file_title": section["file_title"],
                # 被合并块的标题留痕,检索/展示时可还原; 未发生合并的块为空列表
                "merged_titles": section.get("merged_titles", []),
            }
            chunks.append(chunk)
        return chunks

    def _backup_chunks(self, chunks:List[Dict[str,Any]], file_dir:str, file_title:str) -> None:
        """把chunks备份成json文件,便于后续测试观察"""
        #1. file_dir为空时跳过备份
        if not file_dir:
            self.logger.warning("file_dir为空,跳过chunks的备份")
            return
        # 2. 构建备份文件路径: file_dir/文件标题/chunks.json,与文档的其他产物(MD、图片等)放在同一目录下
        backup_dir = Path(file_dir) / file_title
        backup_path = backup_dir / "chunks.json"

        #3. 写入json文件
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(chunks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # 备份失败只告警,不影响主流程
            self.logger.warning(f"备份chunks失败: {e},但是不影响程序的运行")
            return
        self.logger.info(f"chunks备份成功,文件路径是{str(backup_path)}")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    #1. 将md的内容读取出来
    md_path = r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\output_dir\万用表RS-12的使用\万用表RS-12的使用_new.md"

    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    init_state = {
        "md_content": md_content,
        "file_title": "万用表RS-12的使用",
        "file_dir": r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\output_dir"
    }

    document_split_node = DocumentSplitNode()
    result_state = document_split_node(init_state)
    print(f"切分完成,共{len(result_state['chunks'])}个chunks")
