"""Extract readable paragraphs from a public news article page."""

from html.parser import HTMLParser

import requests


SKIP_TAGS = {"script", "style", "nav", "aside", "footer", "noscript", "svg", "form"}


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text_parts = []
        self.paragraphs = {"article": [], "main": []}
        self.in_paragraph = False

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        if tag == "p" and not any(parent in SKIP_TAGS for parent in self.stack):
            self.in_paragraph = True
            self.text_parts = []

    def handle_data(self, data):
        if self.in_paragraph and not any(parent in SKIP_TAGS for parent in self.stack):
            self.text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self.in_paragraph:
            value = " ".join(" ".join(self.text_parts).split())
            if len(value) >= 35:
                for container in ("article", "main"):
                    if container in self.stack:
                        self.paragraphs[container].append(value)
            self.in_paragraph = False
            self.text_parts = []
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]


def article_text(url, max_chars=6500):
    """Return article text, or an empty string if extraction lacks usable detail."""
    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (MorningBird personal news reader)"})
    response.raise_for_status()
    parser = ArticleParser()
    parser.feed(response.text)
    paragraphs = parser.paragraphs["article"] if len(parser.paragraphs["article"]) >= 3 else parser.paragraphs["main"]
    text = "\n".join(dict.fromkeys(paragraphs))
    if len(text) < 300:
        return ""
    if len(text) <= max_chars:
        return text
    start = text[: max_chars - 1700].rsplit("\n", 1)[0]
    end = text[-1500:].split("\n", 1)[-1]
    return start + "\n[Middle of article omitted for length.]\n" + end
