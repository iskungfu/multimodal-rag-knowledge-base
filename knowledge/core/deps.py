from functools import cache

from knowledge.service.file_process_service import FileProcessService


@cache
def get_file_process_service():
    return FileProcessService()
