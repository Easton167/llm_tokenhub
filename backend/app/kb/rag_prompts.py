"""Chat RAG 提示词模板（中文 UTF-8）。

编排层将命中分块填入模板后，作为 system 消息注入，再拼接对话历史与用户问题。
"""

from __future__ import annotations

# 命中知识库时的系统提示词；{knowledge_block} 为带编号的参考资料正文。
RAG_HIT_SYSTEM_PROMPT = """你是一个基于知识库回答问题的助手。请优先依据下列参考资料回答用户问题；在回答中用 [1]、[2] 等形式标注引用来源编号。若参考资料不足以完整回答，请明确说明哪些部分无法从资料中确认。

## 参考资料
{knowledge_block}
"""

# 单条参考资料块模板
RAG_KNOWLEDGE_ITEM_TEMPLATE = "[{index}] 《{document_name}》\n{content}"

# fallback 未命中：允许模型自答，并要求标注未引用知识库
RAG_FALLBACK_MISS_SYSTEM_PROMPT = (
    "本次未从用户挂载的知识库中检索到相关内容。"
    "请基于通用知识回答用户问题，并在回答中明确说明：本次回答未引用知识库。"
)
