"""结构优先 + 长度约束分块器。"""

from __future__ import annotations

from app.kb.errors import InvalidChunkParamsError
from app.kb.tokens import estimate_tokens
from app.kb.types import (
    Chunk,
    ElementKind,
    ParsedDocument,
    ParsedElement,
    SourceLocation,
)

CHUNK_SIZE_MIN = 100
CHUNK_SIZE_MAX = 2000
CHUNK_OVERLAP_MIN = 0
CHUNK_OVERLAP_MAX = 200

# 默认分隔符优先级：段落 → 换行 → 中英文句读 → 空白 → 硬切
DEFAULT_SEPARATORS: tuple[str, ...] = (
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    ". ",
    "! ",
    "? ",
    "; ",
    " ",
    "",
)


def chunk(
    parsed_doc: ParsedDocument,
    chunk_size: int,
    chunk_overlap: int,
    separators: list[str] | tuple[str, ...] | None = None,
) -> list[Chunk]:
    """将解析结果分块。

    策略：先按标题/段落等结构单元切分并贪心合并；单单元超过 ``chunk_size``
    时按 ``separators`` 二次切分；相邻分块保留 ``chunk_overlap`` token 重叠。

    同一输入与参数下结果确定（可重复）。
    """
    _validate_params(chunk_size, chunk_overlap)
    seps = tuple(separators) if separators is not None else DEFAULT_SEPARATORS
    if not seps:
        seps = DEFAULT_SEPARATORS

    units = _elements_to_units(parsed_doc.elements)
    if not units:
        return []

    # 先把超长单元拆开，得到全部 ≤ chunk_size 的原子片段
    atoms: list[tuple[str, SourceLocation]] = []
    for text, loc in units:
        if estimate_tokens(text) <= chunk_size:
            atoms.append((text, loc))
        else:
            for piece in _split_text(text, chunk_size, seps):
                atoms.append((piece, loc))

    packed = _pack_atoms(atoms, chunk_size)
    return _apply_overlap(packed, chunk_size, chunk_overlap)


def _validate_params(chunk_size: int, chunk_overlap: int) -> None:
    if not isinstance(chunk_size, int) or not isinstance(chunk_overlap, int):
        raise InvalidChunkParamsError("chunk_size and chunk_overlap must be integers")
    if chunk_size < CHUNK_SIZE_MIN or chunk_size > CHUNK_SIZE_MAX:
        raise InvalidChunkParamsError(
            f"chunk_size must be in [{CHUNK_SIZE_MIN}, {CHUNK_SIZE_MAX}], got {chunk_size}"
        )
    if chunk_overlap < CHUNK_OVERLAP_MIN or chunk_overlap > CHUNK_OVERLAP_MAX:
        raise InvalidChunkParamsError(
            f"chunk_overlap must be in [{CHUNK_OVERLAP_MIN}, {CHUNK_OVERLAP_MAX}], "
            f"got {chunk_overlap}"
        )
    if chunk_overlap >= chunk_size:
        raise InvalidChunkParamsError(
            f"chunk_overlap ({chunk_overlap}) must be less than chunk_size ({chunk_size})"
        )


def _elements_to_units(
    elements: tuple[ParsedElement, ...] | list[ParsedElement],
) -> list[tuple[str, SourceLocation]]:
    """标题更新路径；正文/表格行成为带位置信息的结构单元。

    标题文本并入其后首个正文单元前缀，便于检索携带结构上下文。
    """
    units: list[tuple[str, SourceLocation]] = []
    pending_headings: list[str] = []
    heading_stack: list[tuple[int, str]] = []

    for el in elements:
        if el.kind == ElementKind.HEADING:
            level = el.level or 1
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, el.text.strip()))
            pending_headings.append(el.text.strip())
            continue

        text = el.text.strip()
        if not text:
            continue
        path = (
            el.location.heading_path
            if el.location.heading_path
            else tuple(t for _, t in heading_stack)
        )
        if pending_headings:
            prefix = " / ".join(pending_headings)
            text = f"{prefix}\n{text}"
            pending_headings.clear()
        loc = SourceLocation(
            page=el.location.page,
            heading_path=path,
            row_index=el.location.row_index,
            sheet=el.location.sheet,
        )
        units.append((text, loc))

    # 仅有标题无正文时，标题自身作为单元
    if pending_headings:
        path = tuple(t for _, t in heading_stack)
        units.append(
            (
                " / ".join(pending_headings),
                SourceLocation(heading_path=path),
            )
        )
    return units


def _pack_atoms(
    atoms: list[tuple[str, SourceLocation]], chunk_size: int
) -> list[tuple[str, SourceLocation]]:
    """贪心合并相邻原子片段，使每个包 ≤ chunk_size。"""
    packed: list[tuple[str, SourceLocation]] = []
    buf_parts: list[str] = []
    buf_loc: SourceLocation | None = None

    def flush() -> None:
        nonlocal buf_parts, buf_loc
        if not buf_parts:
            return
        content = "\n".join(buf_parts)
        packed.append((content, buf_loc or SourceLocation()))
        buf_parts = []
        buf_loc = None

    for text, loc in atoms:
        if not buf_parts:
            buf_parts = [text]
            buf_loc = loc
            continue
        trial = "\n".join(buf_parts + [text])
        if estimate_tokens(trial) <= chunk_size:
            buf_parts.append(text)
            continue
        flush()
        buf_parts = [text]
        buf_loc = loc

    flush()
    return packed


def _apply_overlap(
    packed: list[tuple[str, SourceLocation]],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    if not packed:
        return []
    if chunk_overlap <= 0:
        return [
            Chunk(
                index=i,
                content=text,
                token_count=estimate_tokens(text),
                source_location=loc,
            )
            for i, (text, loc) in enumerate(packed)
        ]

    result: list[Chunk] = []
    prev_text = ""
    for i, (text, loc) in enumerate(packed):
        if i == 0 or not prev_text:
            content = text
        else:
            overlap_text = _suffix_by_tokens(prev_text, chunk_overlap)
            if overlap_text:
                trial = f"{overlap_text}\n{text}"
                # 保证最终块不超过 chunk_size：必要时缩短正文尾部之前的新内容
                if estimate_tokens(trial) <= chunk_size:
                    content = trial
                else:
                    # 保留 overlap，截断新文本
                    budget = chunk_size - estimate_tokens(overlap_text)
                    if budget <= 0:
                        content = _prefix_by_tokens(overlap_text, chunk_size)
                    else:
                        trimmed = _prefix_by_tokens(text, budget)
                        content = (
                            f"{overlap_text}\n{trimmed}" if trimmed else overlap_text
                        )
                        # 二次保险
                        if estimate_tokens(content) > chunk_size:
                            content = _prefix_by_tokens(content, chunk_size)
            else:
                content = text

        # 硬约束：token 数不超过 chunk_size
        if estimate_tokens(content) > chunk_size:
            content = _prefix_by_tokens(content, chunk_size)

        result.append(
            Chunk(
                index=i,
                content=content,
                token_count=estimate_tokens(content),
                source_location=loc,
            )
        )
        prev_text = content
    return result


def _split_text(text: str, chunk_size: int, separators: tuple[str, ...]) -> list[str]:
    """按分隔符优先级递归切分，直到每段 token ≤ chunk_size。"""
    text = text.strip()
    if not text:
        return []
    if estimate_tokens(text) <= chunk_size:
        return [text]

    for sep in separators:
        if sep == "":
            return _hard_split(text, chunk_size)
        if sep not in text:
            continue
        parts = text.split(sep)
        # 还原分隔符到段尾（最后一段除外），保持语义完整
        pieces: list[str] = []
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                piece = part + sep
            else:
                piece = part
            piece = piece.strip()
            if piece:
                pieces.append(piece)
        if len(pieces) <= 1:
            continue
        # 将小片段再贪心合并到 ≤ chunk_size
        return _merge_pieces(pieces, chunk_size, separators)
    return _hard_split(text, chunk_size)


def _merge_pieces(
    pieces: list[str], chunk_size: int, separators: tuple[str, ...]
) -> list[str]:
    merged: list[str] = []
    buf = ""
    for piece in pieces:
        if estimate_tokens(piece) > chunk_size:
            if buf:
                merged.append(buf)
                buf = ""
            merged.extend(_split_text(piece, chunk_size, separators))
            continue
        trial = f"{buf}{piece}" if buf else piece
        if buf and estimate_tokens(trial) > chunk_size:
            merged.append(buf)
            buf = piece
        else:
            buf = trial
    if buf:
        merged.append(buf)
    return merged


def _hard_split(text: str, chunk_size: int) -> list[str]:
    """按字符硬切：chunk_size token ≈ chunk_size * 4 字符。"""
    width = max(1, chunk_size * 4)
    return [text[i : i + width] for i in range(0, len(text), width) if text[i : i + width]]


def _suffix_by_tokens(text: str, n: int) -> str:
    if n <= 0 or not text:
        return ""
    # 从尾部扩展字符直到 token 达标
    char_step = 4
    size = min(len(text), max(char_step, n * char_step))
    candidate = text[-size:]
    while size < len(text) and estimate_tokens(candidate) < n:
        size = min(len(text), size + char_step)
        candidate = text[-size:]
    # 若略超，尽量从左侧对齐到空白/句读
    if estimate_tokens(candidate) > n and len(candidate) > char_step:
        # 缩到刚好 ≥ n
        while len(candidate) > char_step and estimate_tokens(candidate[char_step:]) >= n:
            candidate = candidate[char_step:]
    return candidate


def _prefix_by_tokens(text: str, n: int) -> str:
    if n <= 0 or not text:
        return ""
    char_step = 4
    size = min(len(text), max(char_step, n * char_step))
    candidate = text[:size]
    while size < len(text) and estimate_tokens(candidate) < n:
        size = min(len(text), size + char_step)
        candidate = text[:size]
    while estimate_tokens(candidate) > n and len(candidate) > 0:
        candidate = candidate[:-char_step] if len(candidate) > char_step else candidate[:-1]
    return candidate
