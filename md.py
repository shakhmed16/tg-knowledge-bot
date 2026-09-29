"""Markdown из ответа ИИ -> HTML, который понимает Telegram.

Модели отвечают в Markdown (**жирный**, `код`, ### заголовки). Telegram такую
разметку не разбирает, поэтому в чат прилетали звёздочки. Здесь она переводится
в поддерживаемые Telegram теги, а на случай сбоя есть strip() — просто убрать
разметку.
"""
from __future__ import annotations

import html
import re

_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+#-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*#*$", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD_ALT = re.compile(r"__(.+?)__", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_ALT = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^([ \t]*)[*+-][ \t]+", re.M)
_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.M)


def to_html(text: str) -> str:
    """Markdown -> HTML для Telegram (parse_mode='HTML')."""
    blocks: list[str] = []

    def stash_block(m: re.Match) -> str:
        blocks.append(f"<pre><code>{html.escape(m.group(1).strip())}</code></pre>")
        return f"\x00{len(blocks) - 1}\x00"

    def stash_inline(m: re.Match) -> str:
        blocks.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(blocks) - 1}\x00"

    text = _CODE_BLOCK.sub(stash_block, text or "")
    text = _INLINE_CODE.sub(stash_inline, text)

    text = html.escape(text)

    text = _HR.sub("─────", text)
    text = _HEADER.sub(r"<b>\1</b>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_ALT.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _ITALIC_ALT.sub(r"<i>\1</i>", text)
    text = _BULLET.sub(r"\1• ", text)

    for i, block in enumerate(blocks):
        text = text.replace(f"\x00{i}\x00", block)
    return text.strip()


def strip(text: str) -> str:
    """Просто убрать разметку — запасной вариант, если HTML не принят."""
    text = _CODE_BLOCK.sub(lambda m: m.group(1).strip(), text or "")
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HR.sub("─────", text)
    text = _HEADER.sub(r"\1", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_ALT.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _ITALIC_ALT.sub(r"\1", text)
    text = _BULLET.sub(r"\1• ", text)
    return text.strip()
