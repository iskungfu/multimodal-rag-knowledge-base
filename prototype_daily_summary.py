from openai import OpenAI
import json

client = OpenAI(base_url='http://127.0.0.1:3425/v1', api_key='magpie')

# 模拟收盘后行情数据
market_snapshot = {
    'date': '2026-11-26',
    'indexes': [
        {'name': '上证指数', 'point': 3476.88, 'change_pct': 1.00, 'volume': 6133},
        {'name': '深证成指', 'point': 10890.50, 'change_pct': -0.30, 'volume': 5200},
        {'name': '创业板指', 'point': 2180.30, 'change_pct': -0.80, 'volume': 3100},
    ],
    'watchlist': [
        {'code': '600519', 'name': '贵州茅台', 'price': 1469.00, 'change_pct': -4.00, 'pe': 58.4, 'turnover_rate': 26},
        {'code': '300750', 'name': '宁德时代', 'price': 204.22, 'change_pct': 2.50, 'pe': 45.0, 'turnover_rate': 15},
        {'code': '000001', 'name': '平安银行', 'price': 12.50, 'change_pct': -1.20, 'pe': 6.2, 'turnover_rate': 8},
    ],
}

# 用户个人策略片段（从 Milvus personal 库召回）
personal_strategy = [
    '熊市轻仓只做龙头股，仓位不超过 20%，随时准备止损。',
    'RSI 超卖时可少量试探买入，但不要追高。',
    '上次追高贵州茅台被套，教训是没等支撑位就冲进去，心态很急。下次要等企稳再买。',
]

# 公共知识片段（从 Milvus knowledge 库召回）
public_knowledge = [
    '安全边际是价值投资的核心，买入价应低于内在价值，避免在市场乐观过度时追高。',
    '牛市中持有龙头股，熊市中减仓观望，不要试图抄底个股。',
]

SYSTEM_PROMPT = '''你是一个个人投资顾问助手。请根据收盘后的行情快照、用户个人策略和公共投资知识，生成一篇每日投资摘要。

要求：
1. 简洁明了，普通投资者一眼看懂（建议 200-400 字）
2. 包含：大盘位置简评、关注个股风险提示、基于个人策略的操作建议
3. 明确引用信息来源（“根据行情数据……”、“根据你的个人策略……”、“根据投资经典……”）
4. 结尾强制附加免责声明：“以上信息不构成投资建议，投资有风险，入市需谨慎。”
5. 语言通俗易懂，不要使用过多专业术语
'''

user_prompt = '''今日收盘行情快照：
日期：{date}
大盘：
{indexes}

关注个股：
{watchlist}

你的个人策略：
{personal}

投资经典理论：
{public}

请生成每日投资摘要。'''.format(
    date=market_snapshot['date'],
    indexes='\\n'.join(['- {}：点位 {:.2f}，涨跌幅 {:.2f}%，成交额 {} 亿元'.format(i['name'], i['point'], i['change_pct'], i['volume']) for i in market_snapshot['indexes']]),
    watchlist='\\n'.join(['- {}（{}）：价格 {:.2f}，涨跌幅 {:.2f}%，PE {:.1f}，换手率 {:.0f}%'.format(s['name'], s['code'], s['price'], s['change_pct'], s['pe'], s['turnover_rate']) for s in market_snapshot['watchlist']]),
    personal='\\n'.join(['- ' + s for s in personal_strategy]),
    public='\\n'.join(['- ' + s for s in public_knowledge]),
)

print('=== 每日投资摘要生成 ===')
print()
print('--- 输入摘要 ---')
print(user_prompt[:600])
print()

try:
    resp = client.chat.completions.create(
        model='group/group',
        temperature=0.3,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_prompt},
        ],
    )
    summary = resp.choices[0].message.content.strip()
    print('--- 生成摘要 ---')
    print(summary)
    print()
    print('字数：{}'.format(len(summary)))
except Exception as e:
    print('调用失败: {}: {}'.format(type(e).__name__, e))
