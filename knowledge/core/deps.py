from functools import cache


@cache
def get_file_process_service():
    from knowledge.service.file_process_service import FileProcessService
    return FileProcessService()


@cache
def get_query_service():
    from knowledge.service.query_service import QueryService
    return QueryService()
