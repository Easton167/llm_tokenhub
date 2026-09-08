"""Token 估算：与网关计费预估口径一致（约 4 字符 ≈ 1 token）。"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数。

    与 ``app.api.v1.embeddings._estimate_input_tokens`` /
    ``app.api.v1.chat._estimate_input_tokens`` 相同：``max(1, len // 4)``；
    空串返回 0（分块场景不产生空块）。
    """
    if not text:
        return 0
    return max(1, len(text) // 4)
