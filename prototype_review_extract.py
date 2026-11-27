from openai import OpenAI
import json, re

client = OpenAI(base_url='http://127.0.0.1:3425/v1', api_key='magpie')

samples = [
    '今天 12.5 追高买了贵州茅台，被套后反思没等支撑位就冲进去，心态很急。',
    '昨天卖出了宁德时代，在 185 卖掉，获利了结，之后股价继续涨，有点后悔卖早了。',
    '牛市时仓位重了 80%，现在大盘见顶了，准备减仓到 40%，防控回调风险。',
    '买入了 000001 上证指数 ETF，低位布局，长期持有，不看短线波动。',
    '今天买了一只不知名的小盘股，完全是跟风，没看基本面，亏了 5%，教训深刻。',
    '熊市轻仓只做龙头股，今天买了 600519，仓位只开 20%，随时准备止损。',
]

SYSTEM_PROMPT = '''你是一个金融交易复盘助手。请从用户的自然语言复盘记录中抽取结构化字段，并以 JSON 格式返回：
{
  "stock_code": "股票代码，如 600519，未提及则为 null",
  "stock_name": "股票名称，未提及则为 null",
  "direction": "买入/卖出/持有，未提及则为 null",
  "price": "买卖价格数字，未提及则为 null",
  "reason": "买卖原因简述",
  "lesson": "反思教训简述",
  "emotion": "情绪标签，如 焦虑/后悔/冷静/贪婪/恐惧，未提及则为 null",
  "position": "仓位比例数字，如 80，未提及则为 null"
}
只返回 JSON，不要额外解释。'''

def extract(text):
    resp = client.chat.completions.create(
        model='group/group',
        temperature=0.1,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': text},
        ],
    )
    content = resp.choices[0].message.content.strip()
    content = re.sub(r'^`json\n|`$', '', content, flags=re.MULTILINE).strip()
    try:
        return json.loads(content)
    except Exception:
        # 尝试提取 JSON 子串
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return {'_raw': content, '_parse_error': True}

for i, text in enumerate(samples, 1):
    print()
    print('=== 样本 {} ==='.format(i))
    print('原文: {}'.format(text))
    try:
        result = extract(text)
        print('抽取结果: {}'.format(json.dumps(result, ensure_ascii=False, indent=2)))
    except Exception as e:
        print('调用失败: {}: {}'.format(type(e).__name__, e))
