import os
from openai import OpenAI

input_text = "衣服的质量杠杠的"

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://llm-9489u17xxre3z8o2.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)

completion = client.embeddings.create(
    model="text-embedding-v4",
    input=input_text
)

print(completion.model_dump_json())
print(len(completion.data[0].embedding))