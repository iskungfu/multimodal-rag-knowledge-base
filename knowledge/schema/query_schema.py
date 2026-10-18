"""查询相关 Schema 定义"""

from typing import Optional, List, Dict
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """POST /query 请求体"""
    query: str = Field(..., description="查询内容")
    session_id: Optional[str] = Field(None, description="会话ID，不传则自动生成")
    is_stream: bool = Field(False, description="是否流式返回")


class QueryResponse(BaseModel):
    """POST /query 非流式响应"""
    message: str = Field(..., description="响应消息")
    session_id: str = Field(..., description="会话ID")
    answer: str = Field("", description="生成的答案")
    done_list: List[str] = Field(default_factory=list, description="已完成节点列表(中文名)")
    error: str = Field("", description="错误信息，空字符串表示成功")
    image_urls: List[str] = Field(default_factory=list, description="答案引用的图片URL")


class StreamSubmitResponse(BaseModel):
    """POST /query 流式响应"""
    message: str = Field(..., description="响应消息")
    session_id: str = Field(..., description="会话ID")
    task_id: str = Field(..., description="任务ID，前端用此 ID 建立 SSE 连接")


class QueryStatusResponse(BaseModel):
    """GET /status/{task_id} 响应"""
    status: str = Field("", description="任务状态: processing/completed/failed")
    done_list: List[str] = Field(default_factory=list, description="已完成节点(中文名)")
    running_list: List[str] = Field(default_factory=list, description="运行中节点(中文名)")
    durations: Dict[str, float] = Field(default_factory=dict, description="各节点耗时(秒)")
    answer: str = Field("", description="答案，完成后有值")
    error: str = Field("", description="错误信息")


class HistoryItem(BaseModel):
    """单条历史消息"""
    id: str = Field("", alias="_id")
    session_id: str = ""
    role: str = ""
    text: str = ""
    rewritten_query: str = ""
    # 可变默认值必须用 default_factory,否则默认值会是 list 类型对象而不是空列表
    item_names: List[str] = Field(default_factory=list)
    ts: Optional[float] = None


class HistoryResponse(BaseModel):
    """GET /history/{session_id} 响应"""
    session_id: str
    items: List[HistoryItem]
