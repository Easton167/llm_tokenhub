from openai import OpenAI

client = OpenAI(
    api_key="sk-pG-_97xQEALnm_xB-gPni3hZvU98VWMOFzQjkEC4Zlo", 
    base_url="http://127.0.0.1:8000/v1")

response = client.chat.completions.create(
    model="qwen3.7-max",
    messages=[{"role": "user", "content": "你好"}],
)

models = client.models.list()
print(models)
print(response.choices[0].message.content)