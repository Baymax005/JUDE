from __future__ import annotations

import re

import trafilatura
from ddgs import DDGS

from app.tier2.ollama_client import OllamaClient

_SEARCH_PREFIX_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:uh\s+)?(?:please\s+|can\s+you\s+)?"
    r"(?:search\s+(?:the\s+(?:web|internet)|duckduckgo|online)(?:\s+for)?|"
    r"look\s+up(?:\s+online|\s+on\s+the\s+web)?|find\s+(?:out\s+)?(?:online|on\s+the\s+web)?|"
    r"check\s+(?:online|the\s+web)(?:\s+for)?)\s*[:,-]?\s*",
    flags=re.IGNORECASE,
)


def clean_search_query(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    stripped = _SEARCH_PREFIX_RE.sub("", cleaned).strip(" ?.")
    stripped = re.sub(r"\s+(?:online|on\s+the\s+web)$", "", stripped, flags=re.I).strip()
    return stripped or cleaned


def run_web_search(text: str, ollama: OllamaClient, max_results: int = 4) -> tuple[str, list[tuple[str, str]]]:
    """Search DuckDuckGo, extract top pages with trafilatura, and summarize via Ollama."""
    query = clean_search_query(text)
    try:
        raw_results = DDGS().text(query, max_results=max_results) or []
    except Exception as exc:
        return f"Web search failed for '{query}': {exc}", []

    if not raw_results:
        return f"No web results were returned for '{query}'.", []

    sources: list[tuple[str, str]] = []
    context_blocks: list[str] = []

    for idx, item in enumerate(raw_results[:max_results], start=1):
        title = str(item.get("title", "Untitled")).strip()
        url = str(item.get("href") or item.get("url") or "").strip()
        snippet = str(item.get("body") or item.get("snippet") or "").strip()
        if url:
            sources.append((title, url))

        page_excerpt = ""
        if idx <= 2 and url:
            try:
                downloaded = trafilatura.fetch_url(url)
                if downloaded:
                    extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                    if extracted:
                        page_excerpt = " ".join(extracted.split())[:1200]
            except Exception:
                page_excerpt = ""

        block = f"[{idx}] {title} ({url})\nSnippet: {snippet}"
        if page_excerpt:
            block += f"\nExcerpt: {page_excerpt}"
        context_blocks.append(block)

    prompt = (
        f"User question: {query}\n\n"
        "Web search context:\n"
        + "\n\n".join(context_blocks)
        + "\n\nAnswer concisely in 2-4 sentences using ONLY the provided search context."
    )
    summary = ollama.chat(prompt, temperature=0.2, max_tokens=260)

    if sources:
        src_lines = "\n".join(f"- [{title}]({url})" for title, url in sources[:3])
        return f"{summary}\n\nSources:\n{src_lines}", sources
    return summary, sources
