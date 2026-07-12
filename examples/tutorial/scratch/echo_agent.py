import re

# 定义模型协议
class ModelClient:
    def complete(self, prompt: str, max_new_tokens: int = 512) -> str:
        raise NotImplementedError
# 定义Echo模型，验证runtime
class EchoModel(ModelClient):
    def complete(self, prompt: str, max_new_tokens: int = 512) -> str:
        return f"<final>{prompt}</final>"


# runtime只负责调用模型和解析模型的输出，不关心模型的具体实现
# 没有工具时，agent只能回答，不能观察文件系统
class MiniRuntime:
    # 模型client是可替换的，不会写死在runtime里，可以替换
    def __init__(self, model_client: ModelClient):
        self.model_client = model_client

    def ask(self, user_message: str) -> str:
        raw = self.model_client.complete(user_message)
        return self.parse_final(raw)

    def parse_final(self, raw: str) -> str:
        # 使用正则表达式匹配<final>标签内的内容
        match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
        if not match:
            raise ValueError("model did not return <final>")
        return match.group(1).strip() #group(1)是匹配到的内容，strip()去掉前后空格,，group(0)是匹配到的整个字符串
if __name__ == "__main__":
    agent = MiniRuntime(EchoModel())
    print(agent.ask("hello pico"))