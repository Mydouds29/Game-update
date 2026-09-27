"""RawPatch -> Patch / Section / Item, format commun à toutes les sources."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup, NavigableString, Tag

from ..collectors.base import RawPatch
from ..collectors.blizzard import parse_heading

MAX_ITEMS = 3000
MAX_ITEM_CHARS = 4000
MAX_SUBGROUP_CHARS = 120
# Contenu placé avant le premier titre : section sans titre (aucun titre inventé,
# le patch note est affiché tel que publié).
DEFAULT_SECTION = ""

BULLET_RE = re.compile(r"^\s*(?:[-–—•*·▪►]|\d+[.)])\s+")
CONTAINER_TAGS = {"div", "section", "article", "main", "blockquote", "span", "center",
                  "font", "figure", "details", "summary", "header", "footer", "aside"}
SKIP_TAGS = {"script", "style", "noscript", "img", "video", "iframe", "svg", "picture",
             "figcaption", "button", "form", "input", "nav"}
HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


@dataclass
class Item:
    text: str
    subgroup: str | None = None
    kind: str = "change"


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)


@dataclass
class Patch:
    title: str
    source_key: str
    source_url: str
    version: str | None
    build: str | None
    platforms: str | None
    published_at: datetime | None
    published_raw: str | None
    sections: list[Section]
    content_hash: str

    @property
    def item_count(self) -> int:
        return sum(1 for s in self.sections for i in s.items if i.kind != "note")


# -- BBCode ----------------------------------------------------------------

_BB_REMOVE_BLOCK = re.compile(
    r"\[(img|previewyoutube|video|dynamiclink)(=[^\]]*)?\].*?\[/\1\]", re.I | re.S
)
_BB_SIMPLE = {
    r"h1": "h1", r"h2": "h2", r"h3": "h3", r"h4": "h4", r"h5": "h5", r"h6": "h6",
    r"list": "ul", r"olist": "ol", r"p": "p", r"table": "table", r"tr": "tr",
    r"td": "td", r"th": "td", r"b": "strong", r"quote": "blockquote",
    r"code": "code", r"expand": "div", r"spoiler": "span",
}
_BB_STRIP = re.compile(
    r"\[/?(i|u|s|strike|noparse|hr|url|color|size|emoticon|tbody|thead)(=[^\]]*)?\]", re.I
)


def bbcode_to_html(text: str) -> str:
    text = _BB_REMOVE_BLOCK.sub("", text)
    text = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", "", text)
    text = html.escape(text, quote=False)
    for bb, tag in _BB_SIMPLE.items():
        text = re.sub(rf"\[{bb}(=[^\]]*)?\]", f"<{tag}>", text, flags=re.I)
        text = re.sub(rf"\[/{bb}\]", f"</{tag}>", text, flags=re.I)
    text = re.sub(r"\[\*\]", "<li>", text)
    text = re.sub(r"\[/\*\]", "</li>", text)
    text = _BB_STRIP.sub("", text)
    return text.replace("\r\n", "\n").replace("\n", "<br>")


# -- extraction -------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def item_kind(text: str, section_title: str = "") -> str:
    lowered = text.lower()
    if re.match(r"(fixed|fix|fixes|resolved|addressed|corrected)\b", lowered):
        return "fix"
    if re.match(r"(added|new|introduced|adds)\b", lowered):
        return "new"
    if "balance" in section_title.lower() or re.search(
        r"\b(increased|decreased|reduced|buffed|nerfed)\b.*\d", lowered
    ):
        return "balance"
    return "change"


class _Builder:
    def __init__(self, section_levels: tuple[int, int]) -> None:
        self.sections: list[Section] = []
        self.subgroup: str | None = None
        self.section_level, self.subgroup_level = section_levels
        self.count = 0

    @property
    def current(self) -> Section:
        if not self.sections:
            self.sections.append(Section(DEFAULT_SECTION))
        return self.sections[-1]

    def start_section(self, title: str) -> None:
        title = _clean(title)[:200]
        if not title:
            return
        # Un titre de section sans contenu est remplacé par le suivant.
        if self.sections and not self.sections[-1].items:
            self.sections[-1].title = title
        else:
            self.sections.append(Section(title))
        self.subgroup = None

    def set_subgroup(self, title: str | None) -> None:
        self.subgroup = _clean(title)[:MAX_SUBGROUP_CHARS] if title else None

    def add(self, text: str, *, subgroup: str | None = None, note: bool = False) -> None:
        text = BULLET_RE.sub("", _clean(text))
        if not text or self.count >= MAX_ITEMS:
            return
        section = self.current
        kind = "note" if note else item_kind(text, section.title)
        section.items.append(Item(
            text=text[:MAX_ITEM_CHARS],
            subgroup=subgroup if subgroup is not None else self.subgroup,
            kind=kind,
        ))
        self.count += 1


def _own_text(li: Tag) -> str:
    parts = []
    for child in li.children:
        if isinstance(child, Tag) and child.name in {"ul", "ol"}:
            continue
        parts.append(child.get_text(" ") if isinstance(child, Tag) else str(child))
    return _clean(" ".join(parts))


def _is_label(text: str) -> bool:
    return 0 < len(text) <= 80 and not text.endswith((".", "!", "?"))


def _walk_list(lst: Tag, builder: _Builder, subgroup: str | None) -> None:
    for li in lst.find_all("li", recursive=False):
        text = _own_text(li)
        nested = [c for c in li.children if isinstance(c, Tag) and c.name in {"ul", "ol"}]
        if nested:
            if _is_label(text):
                child_group = f"{subgroup} › {text}" if subgroup else text
            else:
                builder.add(text, subgroup=subgroup)
                child_group = subgroup
            for sub in nested:
                _walk_list(sub, builder, child_group)
        else:
            builder.add(text, subgroup=subgroup)


def _whole_bold(tag: Tag) -> bool:
    text = _clean(tag.get_text(" "))
    if not text:
        return False
    bold = "".join(b.get_text(" ") for b in tag.find_all(["strong", "b"]))
    return _clean(bold) == text


def _flush_lines(lines: list[str], builder: _Builder) -> None:
    for line in lines:
        line = _clean(line)
        if not line:
            continue
        if BULLET_RE.match(line):
            builder.add(line)
        else:
            builder.add(line, note=True)
    lines.clear()


def _walk(node: Tag, builder: _Builder) -> None:
    buffer: list[str] = [""]

    def flush() -> None:
        _flush_lines(buffer, builder)
        buffer.append("")

    for child in node.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in {"Comment", "Doctype", "CData", "Declaration"}:
                continue
            buffer[-1] += str(child)
            continue
        if not isinstance(child, Tag) or child.name in SKIP_TAGS:
            continue
        name = child.name
        if name == "br":
            buffer.append("")
        elif name in HEADING_LEVEL:
            flush()
            level = HEADING_LEVEL[name]
            text = child.get_text(" ")
            if level <= builder.section_level:
                builder.start_section(text)
            else:
                builder.set_subgroup(text)
        elif name in {"ul", "ol"}:
            flush()
            _walk_list(child, builder, builder.subgroup)
        elif name == "table":
            flush()
            for row in child.find_all("tr"):
                cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
                builder.add(" | ".join(c for c in cells if c))
        elif name == "p":
            flush()
            if _whole_bold(child) and _is_label(_clean(child.get_text(" "))):
                label = _clean(child.get_text(" "))
                if builder.sections and builder.sections[-1].items:
                    builder.set_subgroup(label)
                else:
                    builder.start_section(label)
                continue
            # Les <br> d'un paragraphe séparent des lignes indépendantes.
            lines = [""]
            for sub in child.children:
                if isinstance(sub, Tag) and sub.name == "br":
                    lines.append("")
                elif isinstance(sub, Tag) and sub.name in {"ul", "ol"}:
                    _flush_lines(lines, builder)
                    lines.append("")
                    _walk_list(sub, builder, builder.subgroup)
                else:
                    lines[-1] += sub.get_text(" ") if isinstance(sub, Tag) else str(sub)
            _flush_lines(lines, builder)
        elif name in {"strong", "b"} and _is_label(_clean(child.get_text(" "))) \
                and not _clean(buffer[-1]):
            # Libellé en gras seul sur sa ligne (fréquent dans le BBCode Steam).
            nxt = child.next_sibling
            if nxt is None or (isinstance(nxt, Tag) and nxt.name in {"br", "ul", "ol"}):
                flush()
                label = _clean(child.get_text(" "))
                if builder.sections and builder.sections[-1].items:
                    builder.set_subgroup(label)
                else:
                    builder.start_section(label)
                continue
            buffer[-1] += child.get_text(" ")
        elif name in CONTAINER_TAGS or name in {"body", "html"}:
            flush()
            _walk(child, builder)
        else:
            buffer[-1] += child.get_text(" ")
    flush()


def _section_levels(root: Tag) -> tuple[int, int]:
    levels = sorted({HEADING_LEVEL[h.name] for h in root.find_all(list(HEADING_LEVEL))})
    if not levels:
        return (6, 6)
    section = levels[0]
    return (section, levels[1] if len(levels) > 1 else section + 1)


def extract_sections(body_html: str) -> list[Section]:
    soup = BeautifulSoup(f"<div id='gu-root'>{body_html}</div>", "lxml")
    root = soup.find(id="gu-root")
    builder = _Builder(_section_levels(root))
    _walk(root, builder)
    return [s for s in builder.sections if s.items]


def content_hash(title: str, version: str | None, build: str | None,
                 sections: list[Section]) -> str:
    canonical = json.dumps(
        {
            "title": title, "version": version, "build": build,
            "sections": [
                {"t": s.title, "i": [[i.subgroup, i.kind, i.text] for i in s.items]}
                for s in sections
            ],
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize(raw: RawPatch) -> Patch:
    body_html = bbcode_to_html(raw.body) if raw.body_format == "bbcode" else raw.body
    sections = extract_sections(body_html)
    info = parse_heading(raw.title)
    version = raw.version or info["version"]
    build = raw.build or info["build"]
    platforms = raw.platforms or info["platforms"]
    title = _clean(raw.title)[:300]
    return Patch(
        title=title,
        source_key=raw.source_key,
        source_url=raw.url,
        version=version,
        build=build,
        platforms=platforms,
        published_at=raw.published_at,
        published_raw=raw.published_raw,
        sections=sections,
        content_hash=content_hash(title, version, build, sections),
    )
