import html
import re


INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)|(?P<bold>\*\*[^*\n]+\*\*|__[^_\n]+__)"
    r"|(?P<italic>\*(?!\*)[^*\n]+\*)"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>https?://[^\s()]+)\)"
)
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*(?=\n|$)", re.M | re.S)


def _inline(text):
    parts = []
    end = 0
    for match in INLINE.finditer(text):
        parts.append(html.escape(text[end:match.start()]))
        if match["url"]:
            url = html.escape(match["url"], quote=True)
            parts.append(f'<a href="{url}">{html.escape(match["label"])}</a>')
        elif match["code"]:
            parts.append(f'<code>{html.escape(match["code"][1:-1])}</code>')
        elif match["bold"]:
            parts.append(f'<b>{html.escape(match["bold"][2:-2])}</b>')
        else:
            parts.append(f'<i>{html.escape(match["italic"][1:-1])}</i>')
        end = match.end()
    parts.append(html.escape(text[end:]))
    return "".join(parts)


def messages(text):
    """Yield balanced Telegram HTML with a plain-text fallback for each chunk."""
    end = 0
    blocks = []
    for match in FENCE.finditer(text):
        blocks.append((text[end:match.start()], False))
        blocks.append((match[1], True))
        end = match.end()
    blocks.append((text[end:], False))

    pending_html, pending_text = [], []
    length = 0
    for block, is_code in blocks:
        while block:
            cut = min(len(block), 2000)
            if cut < len(block):
                newline = block.rfind("\n", 0, cut)
                if newline >= 0:
                    cut = newline + 1
            part, block = block[:cut], block[cut:]
            if length + len(part) > 2000:
                if "".join(pending_text).strip():
                    yield "".join(pending_html), "".join(pending_text)
                pending_html, pending_text = [], []
                length = 0
            pending_html.append(f"<pre>{html.escape(part)}</pre>" if is_code else _inline(part))
            pending_text.append(part)
            length += len(part)
    if "".join(pending_text).strip():
        yield "".join(pending_html), "".join(pending_text)
