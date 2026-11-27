from openai import OpenAI
import json, re

client = OpenAI(base_url='http://127.0.0.1:3425/v1', api_key='magpie')

PERSONAL_STRATEGY = [
    {"source": "personal", "title": "我的炒股心得", "snippet": "熊市轻仓只做龙头股，仓位不超过 20%，随时准备止损。RSI 超卖时可少量试探买入，但不要追高。"},
    {"source": "personal", "title": "复盘笔记-2024-11", "snippet": "上次追高贵州茅台被套，教训是没等支撑位就冲进去，心态很急。下次要等企稳再买。"},
]
PUBLIC_KNOWLEDGE = [
    {"source": "knowledge", "title": "《聪明的投资者》", "snippet": "安全边际是价值投资的核心，买入价应低于内在价值，避免在市场乐观过度时追高。"},
    {"source": "knowledge", "title": "《股市真规则》", "snippet": "牛市中持有龙头股，熊市中减仓观望，不要试图抄底个股。"},
]
MARKET_DATA = [
    {"source": "market", "title": "贵州茅台行情", "snippet": "当前价 1469.00，涨跌幅 -4.00%，成交量 32408 手，换手率 26%，PE 58.4，处于历史高位。"},
    {"source": "market", "title": "上证指数行情", "snippet": "当前点位 3476.88，涨跌幅 1.00%，成交额 6133 亿元，大盘震荡整理。"},
]

QUERIES = [
    "今天贵州茅台适合买吗？",
    "大盘见顶了，我怎么调整仓位？",
    "RSI 超卖时买入点可靠吗？",
]

SYSTEM_PROMPT = """你是一个个人投资顾问助手。用户提出投资问题，你手头有三路信息：
1. 用户个人策略文档（用户自己的交易经验和心得）
2. 公共投资知识库（经典投资书籍理论）
3. 实时行情数据（个股/大盘当前价格、涨跌幅、PE等）

请综合三路信息，给出个性化投资建议。要求：
- 明确引用信息来源（如“根据你的个人策略……”、“根据《聪明的投资者》……”、“根据行情数据……”）
- 结合用户个人策略与当前行情，给出具体、可操作的建议
- 结尾强制附加免责声明：“以上信息不构成投资建议，投资有风险，入市需谨慎。”
- 语言通俗易懂，面向普通投资者
"""

for i, query in enumerate(QUERIES, 1):
    print()
    print('=== 查询 {} ==='.format(i))
    print('用户问题: {}'.format(query))

    context_parts = []
    for d in PERSONAL_STRATEGY:
        context_parts.append('[个人策略] {}: {}'.format(d['title'], d['snippet']))
    for d in PUBLIC_KNOWLEDGE:
        context_parts.append('[公共知识] {}: {}'.format(d['title'], d['snippet']))
    for d in MARKET_DATA:
        context_parts.append('[行情数据] {}: {}'.format(d['title'], d['snippet']))
    context = '\\n'.join(context_parts)

    print()
    print('--- 三路召回上下文 ---')
    print(context[:800])

    try:
        resp = client.chat.completions.create(
            model='group/group',
            temperature=0.3,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': '\\n'.join(['上下文信息：', context, '', '用户问题：' + query])},
            ],
        )
        answer = resp.choices[0].message.content.strip()
        print()
        print('--- 生成回答 ---')
        print(answer)
    except Exception as e:
        print('调用失败: {}: {}'.format(type(e).__name__, e))
