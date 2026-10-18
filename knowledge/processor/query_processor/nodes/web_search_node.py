import asyncio
import json
from typing import Dict, Any

from agents.mcp import MCPServerStreamableHttp

from knowledge.processor.query_processor.base import BaseNode
from knowledge.processor.query_processor.exceptions import StateFieldError, ConfigurationError
from knowledge.processor.query_processor.state import QueryGraphState


class WebSearchNode(BaseNode):
    name = "web_search_node"

    def process(self, state: QueryGraphState) -> Dict[str, Any]:
        # 0. 校验MCP鉴权配置(Key缺失时请求会直接返回401，提前给出明确错误)
        if not self.config.mcp_dashscope_api_key:
            message = "MCP鉴权Key缺失,请在 knowledge/.env 中配置 MCP_DASHSCOPE_API_KEY(阿里云百炼API Key)"
            self.logger.error(message)
            raise ConfigurationError(node_name=self.name, message=message)

        # 1. 参数校验
        rewritten_query, item_names = self._validate_state(state)
        # 2. 调用mcp工具进行网络检索
        execute_tool_result = asyncio.run(self.mcp_web_search(rewritten_query))
        # 3. 对mcp检索的结果进行格式化
        # 3.1 获取查询结果中的text，这其实就是一个json字符串
        result_text = execute_tool_result.content[0].text
        # 3.2 对json字符串进行反序列化
        json_object = json.loads(result_text)
        # 3.3 取出pages
        pages = json_object["pages"]

        # 声明一个存储web_search结果的容器
        web_search_docs = []
        # 3.4 遍历pages
        for page in pages:
            # 3.4.1 获取snippet、title、url
            snippet = page["snippet"]
            title = page["title"]
            url = page["url"]
            web_search_docs.append({
                "snippet": snippet,
                "title": title,
                "url": url
            })

        # 4. 只返回自己负责的字段(并行节点不能返回整个state,否则会与其他检索节点并发写同一字段)
        return {"web_search_docs": web_search_docs}

    async def mcp_web_search(self, rewritten_query: str):
        # 1. 创建MCP客户端
        async with MCPServerStreamableHttp(
                name="search_mcp",
                params={
                    "url": self.config.mcp_dashscope_base_url,  # MCP 服务端点
                    "headers": {"Authorization": f"Bearer {self.config.mcp_dashscope_api_key}"},  # 认证头(百炼要求 Bearer 前缀)
                    "timeout": 300,  # 请求超时时间（秒）
                    "terminate_on_close": True,  # 关闭时终止连接
                },
                max_retry_attempts=2,  # 最大重试次数
                cache_tools_list=True,  # 缓存工具列表，避免重复请求
        ) as client:
            # 2. 调用mcp中的网络搜索的工具
            execute_tool_result = await client.call_tool(
                tool_name="bailian_web_search",
                arguments={"query": rewritten_query, "count": 3}
            )

            return execute_tool_result

    def _validate_state(self, state: QueryGraphState):
        # 1. 获取rewritten_query,item_names
        rewritten_query = state.get("rewritten_query")
        item_names = state.get("item_names")
        # 2. 校验rewritten_query,item_names
        if not rewritten_query or not isinstance(rewritten_query, str):
            self.logger.error(f"rewritten_query不能为空以及类型必须是str")
            raise StateFieldError(node_name=self.name, field_name="rewritten_query", expected_type=str)

        if not item_names or not isinstance(item_names, list):
            self.logger.error("item_names不能为空以及类型必须是list")
            raise StateFieldError(node_name=self.name, field_name="item_names", expected_type=list)

        return rewritten_query, item_names


if __name__ == '__main__':
    # 1. 创建state
    state = {
        "rewritten_query": "RS PRO RS-12 数字万用表的使用方法是什么?",
        "item_names": ["RS PRO RS-12 数字万用表"]
    }
    node = WebSearchNode()
    final_state = node.process(state)
    print(final_state)
