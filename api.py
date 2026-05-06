"""LLM API 客户端"""
import json
import requests
from config import CHARACTER_INFO


class APIClient:
    def __init__(self, base_url, api_key, model):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })

    def chat(self, character_id: str, message: str, history: list = None) -> str:
        """发送对话，返回回复文本"""
        info = CHARACTER_INFO.get(character_id)
        if not info:
            return "...（我不知道该说什么）"

        messages = [{"role": "system", "content": info["prompt"]}]
        if history:
            messages.extend(history[-6:])  # 保留最近几轮
        messages.append({"role": "user", "content": message})

        try:
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 300
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.Timeout:
            return "...（网络有点慢，你再说一遍？）"
        except requests.exceptions.ConnectionError:
            return "...（连不上——检查一下网络和 API 配置吧）"
        except Exception as e:
            return f"...（出了点岔子：{str(e)[:60]}）"
