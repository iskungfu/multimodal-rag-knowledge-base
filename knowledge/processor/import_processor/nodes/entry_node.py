import logging

from knowledge.processor.import_processor.base import BaseNode, T
from knowledge.processor.import_processor.exceptions import StateFieldError, ValidationError
from knowledge.processor.import_processor.state import ImportGraphState
from pathlib import Path


class EntryNode(BaseNode):
    name = "entry_node"
    def process(self, state: ImportGraphState) -> T:

        #1.获取state中的import_file_path,file_dir,判断是否为空
        import_file_path=state["import_file_path"]
        file_dir=state["file_dir"]
        if not import_file_path:
            self.logger.error("import_file_path is empty")
            raise StateFieldError(node_name=self.name, field_name="import_file_path",expected_type=str,message="import_file_path is empty")
        if not file_dir:
            self.logger.error("file_dir is empty")
            raise StateFieldError(node_name=self.name, field_name="file_dir",expected_type=str,message="file_dir is empty")
        #2.判断import_file_path、file_dir是否真实存在
        import_file_path_obj = Path(import_file_path)
        file_dir_obj = Path(file_dir)
        if not import_file_path_obj.exists():
            self.logger.error(f"import_file_path:{import_file_path} is not exist")
            raise StateFieldError(node_name=self.name, field_name="import_file_path",expected_type=Path,message=f"import_file_path:{import_file_path} is not exist")
        if not file_dir_obj.exists():
            self.logger.error(f"file_dir:{file_dir} is not exist")
            raise StateFieldError(node_name=self.name, field_name="file_dir",expected_type=Path,message=f"file_dir:{file_dir} is not exist")

        #3.判断文件类型
        #3.1获取文件的后缀
        suffix = import_file_path_obj.suffix
        #3.2判断文件的后缀
        if suffix==".pdf":
            state["is_pdf_read_enabled"]=True
            state["pdf_path"]=import_file_path
        elif suffix==".md":
            state["is_md_read_enabled"]=True
            state["md_path"]=import_file_path
        else:
            #3.2.3 抛异常
            self.logger.error(f"unsupported file type:{suffix}")
            raise ValidationError(node_name=self.name,message=f"unsupported file type:{suffix}")

        #4.获取文件标题，并将文件标题设置到state中
        state["file_title"]=import_file_path_obj.stem

        return state

if __name__ == '__main__':
    #测试
    logging.basicConfig(level=logging.DEBUG)
    entry_node = EntryNode()
    init_state={
        "import_file_path": r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\input_dir\万用表RS-12的使用.pdf",
        "file_dir": "D:\Project\shopkeeper-brain\knowledge\processor\import_processor\input_dir"
    }
    state = entry_node.process(state=init_state)
    print(state)
