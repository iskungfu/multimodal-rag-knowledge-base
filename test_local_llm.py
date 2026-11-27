from openai import OpenAI
client = OpenAI(base_url='http://127.0.0.1:3425/v1', api_key='magpie')
r = client.chat.completions.create(
    model='group/group',
    messages=[{'role': 'user', 'content': 'hi'}],
)
print(r.choices[0].message.content)
