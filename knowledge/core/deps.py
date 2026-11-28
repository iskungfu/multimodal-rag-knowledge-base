from functools import cache
from fastapi import HTTPException, Request


@cache
def get_file_process_service():
    from knowledge.service.file_process_service import FileProcessService
    return FileProcessService()


def get_query_service(request: Request = None):
    from knowledge.runtime import runtime_for
    try:
        return runtime_for(request).query
    except Exception:
        raise HTTPException(503, "查询存储暂不可用，请稍后重试") from None
