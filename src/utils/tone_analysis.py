"""Tone-of-conversation analysis for user prompts.

Analyzes the text a user actually typed to Claude Code (not the assistant's
replies) for four signals:
  - profanity        : swear words / cursing            (better-profanity)
  - politeness        : "please" / "thanks" / etc.        (phrase heuristic)
  - spelling mistakes : misspelled English words          (pyspellchecker)
  - grammar mistakes  : a small set of high-confidence, common slip-ups
                        (hand-rolled heuristic -- see _GRAMMAR_RULES)

Design constraints that shaped the approach:
  - This app is explicitly "fully local" (see FastAPI app description) -- no
    network calls to third-party grammar/sentiment APIs, and no Java
    dependency (rules out LanguageTool). Both `better-profanity` and
    `pyspellchecker` ship their word lists/dictionaries in the package
    itself and never touch the network.
  - Claude Code prompts are developer chat, not prose: they're full of file
    paths, identifiers, command flags, and code snippets. A naive spell/
    grammar checker would flag most of that as "wrong". `_extract_words()`
    strips code-shaped tokens before spellchecking, and a small tech-term
    allowlist (_TECH_WHITELIST) covers the common jargon that would
    otherwise still false-positive.
  - Grammar checking intentionally covers only a short list of unambiguous,
    high-confidence mistakes (doubled words, "should of", "your welcome",
    etc.) rather than attempting full sentence-structure grammar checking,
    which would be extremely noisy on lowercase, punctuation-light, code-
    mixed developer messages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List

from better_profanity import profanity as _profanity_checker
from better_profanity.utils import read_wordlist
from spellchecker import SpellChecker

# NOTE: we deliberately do NOT call `_profanity_checker.contains_profanity()`
# per message. That method runs a sliding-window, leetspeak-aware fuzzy
# match against every word in its ~900-word list for every word in the
# input text, and is pathologically slow on real chat-length text (measured
# ~15s for 600 short messages, i.e. multiple seconds per single message).
# Since this analysis runs synchronously per-message across every user's
# prompt history, that was slow enough to stall the request for many
# seconds -- on a single-worker server that blocks the whole event loop,
# so *unrelated* endpoints appeared to hang too, and slow enough on its own
# to make the tone-analysis endpoint itself look "stuck"/crash the
# liveness probe.
#
# Instead we reuse better-profanity's own bundled word list (so it stays
# accurate/up to date with the library) but do a plain, fast lookup: exact
# word-set membership for single-word entries, substring search for the
# small number of multi-word phrases. This is ~6000x faster in practice and
# loses only leetspeak-obfuscation detection (e.g. "sh1t") -- an acceptable
# trade-off for a lightweight internal tone signal, not content moderation.
_profanity_checker.load_censor_words()
_PROFANITY_WORDLIST = list(read_wordlist(_profanity_checker._default_wordlist_filename))
_PROFANITY_WORDS = frozenset(w.lower() for w in _PROFANITY_WORDLIST if " " not in w)
_PROFANITY_PHRASES = [w.lower() for w in _PROFANITY_WORDLIST if " " in w]
_PROFANITY_WORD_RE = re.compile(r"[A-Za-z']+")


def _contains_profanity(text: str) -> bool:
    lowered = text.lower()
    for phrase in _PROFANITY_PHRASES:
        if phrase in lowered:
            return True
    return any(w in _PROFANITY_WORDS for w in _PROFANITY_WORD_RE.findall(lowered))


_spell = SpellChecker(distance=1)  # distance=1 keeps correction search fast

# Common developer/tooling jargon that a general-English dictionary doesn't
# know and would otherwise flag as "misspelled" on nearly every prompt.
_TECH_WHITELIST = {
    "api", "apis", "async", "auth", "backend", "frontend", "npm", "pip",
    "venv", "config", "configs", "json", "yaml", "yml", "html", "css",
    "js", "ts", "jsx", "tsx", "sql", "sqlite", "postgres", "django",
    "flask", "fastapi", "uvicorn", "pytest", "regex", "repo", "repos",
    "readme", "changelog", "cli", "gui", "url", "urls", "uri", "http",
    "https", "endpoint", "endpoints", "middleware", "webhook", "webhooks",
    "kubectl", "kubernetes", "k8s", "docker", "dockerfile", "acr", "aks",
    "schtasks", "powershell", "conhost", "cmdlet", "cmdlets", "env",
    "dotenv", "linting", "lint", "linter", "typo", "typos", "params",
    "arg", "args", "kwargs", "bool", "int", "str", "enum", "iterable",
    "async", "await", "todo", "fixme", "nullable", "boolean", "iterate",
    "iterator", "namespace", "namespaces", "refactor", "refactored",
    "refactoring", "codebase", "changelog", "changelogs", "impl",
    "init", "utils", "util", "stdout", "stderr", "stdin", "regexes",
    "unmount", "unmounted", "rerender", "rerenders", "hoisted", "props",
    "jsx", "tsx", "eslint", "prettier", "webpack", "vite", "npx",
    "changelog", "gitignore", "gitattributes", "monorepo", "submodule",
    "submodules", "upsert", "idempotent", "idempotency", "middleware",
    "backend", "frontend", "fullstack", "devops", "ci", "cd", "cicd",
    "admin", "app", "apps", "booleans", "btw", "checkbox", "dropdown",
    "faq", "faqs", "filepath", "filepaths", "hostname", "hostnames",
    "localhost", "logout", "lol", "metadata", "runtime", "runtimes",
    "screenshot", "screenshots", "signup", "thx", "timestamp",
    "timestamps", "toolkits", "tooltip", "usernames", "wifi", "workflow",
    "workflows", "login", "signup", "setup", "gonna", "wanna", "gotta",
    "yeah", "yep", "nope", "ok", "okay",
}

# Phrases that signal a polite / considerate tone. Matched case-insensitively
# as whole phrases (not just words) to avoid false positives like "please"
# inside an unrelated identifier.
_POLITE_PHRASES = [
    r"\bplease\b", r"\bthanks?\b", r"\bthank you\b", r"\bthx\b",
    r"\bappreciate(?:d|s)?\b", r"\bwould you (?:mind|please)\b",
    r"\bcould you\b", r"\bif (?:it'?s )?possible\b", r"\bsorry\b",
    r"\bno worries\b", r"\bno rush\b", r"\bgreat job\b", r"\bwell done\b",
    r"\bkindly\b", r"\bwhen you (?:get a chance|have (?:a )?time)\b",
    r"\bawesome\b", r"\bnice(?:ly)? done\b",
]
_POLITE_RE = re.compile("|".join(_POLITE_PHRASES), re.IGNORECASE)

# Phrases/patterns that signal a rude/dismissive tone, distinct from outright
# profanity (which better-profanity already covers).
_RUDE_PHRASES = [
    r"\bstupid\b", r"\bidiot(?:ic)?\b", r"\buseless\b", r"\bworthless\b",
    r"\bgarbage\b", r"\btrash\b", r"\bshut up\b", r"\byou suck\b",
    r"\bwhat is wrong with you\b", r"\bhate this\b", r"\bterrible job\b",
    r"\bincompetent\b", r"\bpathetic\b",
]
_RUDE_RE = re.compile("|".join(_RUDE_PHRASES), re.IGNORECASE)

# High-confidence grammar slip-ups only -- see module docstring for why the
# list stays short instead of attempting full grammar checking.
_GRAMMAR_RULES = [
    (re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE), "repeated word"),
    (re.compile(r"\bshould of\b", re.IGNORECASE), "'should of' (should be 'should have')"),
    (re.compile(r"\bcould of\b", re.IGNORECASE), "'could of' (should be 'could have')"),
    (re.compile(r"\bwould of\b", re.IGNORECASE), "'would of' (should be 'would have')"),
    (re.compile(r"\byour welcome\b", re.IGNORECASE), "'your welcome' (should be 'you're welcome')"),
    (re.compile(r"\bi seen\b", re.IGNORECASE), "'i seen' (should be 'i saw'/'i've seen')"),
    (re.compile(r"\bdont'?nt\b", re.IGNORECASE), "double negative contraction"),
    (re.compile(r"\bcould care less\b", re.IGNORECASE), "'could care less' (should be 'couldn't care less')"),
]

# A word is treated as "code-shaped" (skip spellchecking it) if it contains
# any of these: digits, underscores, slashes/backslashes, dots (other than a
# trailing sentence period), camelCase, or is wrapped in backticks.
_CODE_TOKEN_RE = re.compile(r"[0-9_/\\]|[a-z][A-Z]")
_WORD_RE = re.compile(r"[A-Za-z']+")
_BACKTICK_BLOCK_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"https?://\S+")


@dataclass
class ToneResult:
    profanity_hits: int = 0
    rude_hits: int = 0
    polite_hits: int = 0
    spelling_errors: int = 0
    misspelled_words: List[str] = field(default_factory=list)
    grammar_issues: int = 0
    grammar_labels: List[str] = field(default_factory=list)
    words_checked: int = 0


def _looks_code_like(word: str) -> bool:
    """True if `word` is more likely an identifier/path/flag than English
    prose (so it shouldn't be spellchecked)."""
    if len(word) <= 2:
        return True
    if _CODE_TOKEN_RE.search(word):
        return True
    if word.lower() in _TECH_WHITELIST:
        return True
    return False


def _extract_prose_words(text: str) -> List[str]:
    """Pull out the subset of words worth spellchecking: strips backtick
    code spans and URLs first, then filters out anything code-shaped."""
    text = _BACKTICK_BLOCK_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    words = _WORD_RE.findall(text)
    return [w for w in words if not _looks_code_like(w) and w.isalpha()]


@lru_cache(maxsize=4096)
def _is_misspelled(word_lower: str) -> bool:
    return word_lower in _spell.unknown([word_lower])


def analyze_text(text: str) -> ToneResult:
    """Run all four checks on a single message's text."""
    result = ToneResult()
    if not text or not text.strip():
        return result

    result.profanity_hits = 1 if _contains_profanity(text) else 0
    result.rude_hits = len(_RUDE_RE.findall(text))
    result.polite_hits = len(_POLITE_RE.findall(text))

    for pattern, label in _GRAMMAR_RULES:
        matches = pattern.findall(text)
        if matches:
            result.grammar_issues += len(matches)
            result.grammar_labels.append(label)

    prose_words = _extract_prose_words(text)
    result.words_checked = len(prose_words)
    seen = set()
    for word in prose_words:
        lw = word.lower()
        if lw in seen:
            continue
        seen.add(lw)
        if _is_misspelled(lw):
            result.spelling_errors += 1
            if len(result.misspelled_words) < 10:
                result.misspelled_words.append(word)

    return result


def aggregate(results: List[ToneResult]) -> Dict[str, object]:
    """Roll up per-message ToneResults into one summary dict."""
    total_messages = len(results)
    profanity_messages = sum(1 for r in results if r.profanity_hits)
    rude_hits = sum(r.rude_hits for r in results)
    polite_hits = sum(r.polite_hits for r in results)
    spelling_errors = sum(r.spelling_errors for r in results)
    grammar_issues = sum(r.grammar_issues for r in results)
    words_checked = sum(r.words_checked for r in results)
    sample_misspellings = []
    for r in results:
        for w in r.misspelled_words:
            if w not in sample_misspellings:
                sample_misspellings.append(w)
        if len(sample_misspellings) >= 15:
            break

    def _rate(n: int, denom: int) -> float:
        return round((n / denom) * 100, 1) if denom else 0.0

    return {
        "messages_analyzed": total_messages,
        "profanity_messages": profanity_messages,
        "profanity_rate_pct": _rate(profanity_messages, total_messages),
        "rude_phrase_hits": rude_hits,
        "polite_phrase_hits": polite_hits,
        "politeness_rate_pct": _rate(polite_hits, total_messages),
        "spelling_errors": spelling_errors,
        "spelling_error_rate_pct": _rate(spelling_errors, words_checked),
        "grammar_issues": grammar_issues,
        "sample_misspellings": sample_misspellings,
    }


def overall_tone_label(summary: Dict[str, object]) -> Dict[str, str]:
    """A single friendly label + emoji summarizing the aggregate, for quick
    scanning in a table/leaderboard-style view."""
    if summary["messages_analyzed"] == 0:
        return {"label": "No data", "emoji": "\u2796"}
    if summary["profanity_rate_pct"] >= 5 or summary["rude_phrase_hits"] >= 3:
        return {"label": "Needs improvement", "emoji": "\U0001F92C"}
    if summary["politeness_rate_pct"] >= 15:
        return {"label": "Very polite", "emoji": "\U0001F60A"}
    if summary["politeness_rate_pct"] >= 5:
        return {"label": "Polite", "emoji": "\U0001F642"}
    return {"label": "Neutral", "emoji": "\U0001F610"}
