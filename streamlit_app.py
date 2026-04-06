"""
WriteAble
---------------------------------------
This app checks documents for grammar, readability, and accessibility issues.
It gives plain-English explanations for every problem it finds, and lets users
dismiss issues they don't want to act on.
"""

import io
import re
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

import streamlit as st
import textstat

# ── Optional dependencies ─────────────────────────────────────────────────────
# We wrap each import in a try/except so the app still runs even if a package
# isn't installed. The HAS_* flags let us show friendly warnings instead of
# crashing when a feature isn't available.

try:
    from spellchecker import SpellChecker
    _spell = SpellChecker()
    HAS_SPELL = True
except Exception:
    HAS_SPELL = False

try:
    import docx as _docx        # needed to read .docx Word files
    HAS_DOCX = True
except Exception:
    HAS_DOCX = False

try:
    import pdfplumber as _pdfplumber   # needed to read .pdf files
    HAS_PDF = True
except Exception:
    HAS_PDF = False


# ════════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & STYLING
# ════════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="WriteAble – Accessible Document Helper",
    page_icon="📝",
    layout="wide"
)

# All custom CSS lives in one block at the top so it's easy to find and edit.
# We inject it as raw HTML because Streamlit doesn't expose a native theming API
# for things like colored issue rows and severity badges.
st.markdown("""
<style>
html, body, [class*="css"] { font-size: 16px; }
h1, h2, h3 { font-weight: 700; }

/* ── Buttons ── */
.stButton > button {
    background: #005A9E; color: white;
    border: 2px solid #003B6F; border-radius: 4px;
    font-size: 14px;
}
.stButton > button:hover { background: #0078D4; border-color: #005A9E; }

/* ── Issue row strip ──
   Each issue gets a coloured left border to signal severity at a glance.
   The class (error / warning / info) is set dynamically in render_issue(). */
.issue-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-radius: 6px; margin: 5px 0;
    border-left: 5px solid #ccc; background: #fafafa;
}
.issue-row.error   { border-color: #C62828; background: #fff5f5; }
.issue-row.warning { border-color: #E65100; background: #fffbf0; }
.issue-row.info    { border-color: #1565C0; background: #f0f6ff; }

/* ── Small pill badges (ERROR / WARNING / category name) ── */
.badge {
    padding: 2px 9px; border-radius: 12px;
    font-size: 11px; font-weight: 800;
    color: white; white-space: nowrap; display: inline-block;
}
.b-error   { background: #C62828; }
.b-warning { background: #E65100; }
.b-info    { background: #1565C0; }
.b-cat     { background: #444; font-size: 11px; }

/* ── The highlighted text snippet inside an expanded issue ──
   Force black text so it's readable on the light grey background
   regardless of whether the user has a dark-mode OS setting. */
.snippet {
    font-family: monospace; font-size: 13px;
    background: #eeeeee; color: #000000 !important;
    padding: 6px 10px; border-radius: 4px;
    margin: 8px 0; word-break: break-word; white-space: pre-wrap;
}

/* ── Summary stat boxes at the top of the report ── */
.stat-box {
    text-align: center; padding: 18px 10px;
    border-radius: 10px; font-weight: 700; line-height: 1.4;
}
.stat-num { font-size: 32px; display: block; }

textarea, input[type="text"] { border: 1px solid #555 !important; }
</style>
""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════════
# DATA MODEL
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    """
    Represents a single problem found in the document.

    Fields:
        id          – unique integer assigned at check-time (used as widget key base)
        category    – one of: Grammar | Readability | Accessibility
        severity    – one of: error | warning | info  (controls badge colour)
        title       – short one-line description shown in the collapsed row
        explanation – plain-English 'why this matters' shown when expanded
        snippet     – the actual text excerpt that triggered the issue
        suggestion  – optional quick fix string (pre-computed, no AI needed)
    """
    id: int
    category: str
    severity: str
    title: str
    explanation: str
    snippet: str
    suggestion: str = ""


# ════════════════════════════════════════════════════════════════════════════════
# CHECKER ENGINE
# ════════════════════════════════════════════════════════════════════════════════

# Each tuple is (regex pattern, suggested replacement).
# We use word-boundary anchors (\b) so "mankind" doesn't accidentally match
# inside a longer word.
_INCLUSIVE_RULES = [
    (r"\bthe\s+disabled\b",                "people with disabilities"),
    (r"\bthe\s+blind\b",                   "people who are blind"),
    (r"\bthe\s+deaf\b",                    "people who are Deaf or hard of hearing"),
    (r"\bwheelchair[\s\-]?bound\b",        "wheelchair user"),
    (r"\bconfined\s+to\s+a\s+wheelchair\b","wheelchair user"),
    (r"\bsuffers?\s+from\b",               "has / lives with"),
    (r"\bmentally\s+ill\b",                "person with a mental health condition"),
    (r"\bcrippled?\b",                     "person with a disability"),
    (r"\bmankind\b",                       "humanity or humankind"),
    (r"\bmanpower\b",                      "workforce or staffing"),
    (r"\bblacklist\b",                     "blocklist or denylist"),
    (r"\bwhitelist\b",                     "allowlist"),
    (r"\bhe\s+or\s+she\b",                "they"),
    (r"\bhis\s+or\s+her\b",               "their"),
    (r"\bcrazy\b",                         "unexpected / surprising"),
    (r"\binsane\b",                        "extreme / unreasonable"),
    (r"\bdumb\b",                          "confusing or unclear"),
    (r"\bstupid\b",                        "unclear / poorly designed"),
    (r"\blow[\s\-]functioning\b",          "person who needs significant support"),
    (r"\bhigh[\s\-]functioning\b",         "person who needs minimal support"),
    (r"\bnormal\s+people\b",              "people without disabilities"),
]

# Detects common passive-voice constructions like "was reviewed", "is being done".
# Not 100% accurate, passive voice isn't always wrong, but heavy use is flagged.
_PASSIVE_RE = re.compile(
    r'\b(?:am|is|are|was|were|be|been|being)\s+\w+(?:ed|en)\b',
    re.IGNORECASE
)

# Matches sequences of 2–7 uppercase letters (potential acronyms like WCAG, HR, PDF).
_ACRONYM_RE = re.compile(r'\b([A-Z]{2,7})\b')


def _word_count(text: str) -> int:
    """Count only real words, ignores punctuation and numbers."""
    return len(re.findall(r'\b[a-zA-Z]+\b', text))


def _sentences(text: str) -> List[str]:
    """
    Split text into sentences on . ! ? boundaries.
    Returns a list of non-empty stripped sentence strings.
    """
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]


def run_checks(text: str) -> List[Issue]:
    """
    Master function that runs all 12 checks and returns a list of Issue objects.

    Checks are grouped roughly as:
        1–3   Grammar      (spelling, repeated words, extra spaces)
        4–6   Readability  (sentence length, Flesch scores, passive voice)
        7–12  Accessibility (inclusive language, ALL CAPS, acronyms, headings,
                             paragraph structure, bare URLs)
    """
    issues: List[Issue] = []
    _id = 0  # auto-incrementing issue ID

    # Small helper so we don't repeat the Issue(...) constructor everywhere
    def add(cat, sev, title, expl, snip, sug=""):
        nonlocal _id
        _id += 1
        issues.append(Issue(_id, cat, sev, title, expl, snip, sug))

    # Pre-compute some useful slices of the text we'll reuse across checks
    all_words   = re.findall(r'\b[a-zA-Z]+\b', text)
    sentences   = _sentences(text)
    total_words = _word_count(text)

    # Check 1: Spelling
    # We only check lowercase words longer than 3 characters.
    # This skips proper nouns (capitalized) and short words like "the" or "is",
    # which the spell checker tends to flag incorrectly for domain-specific text.
    if HAS_SPELL:
        check_words = [w for w in all_words if not w[0].isupper() and len(w) > 3]
        misspelled  = _spell.unknown(check_words)
        for word in sorted(misspelled)[:12]:   # cap at 12 to avoid overwhelming the report
            correction = _spell.correction(word)
            if correction and correction != word:
                add("Grammar", "error",
                    f"Possible misspelling: '{word}'",
                    f"Did you mean '{correction}'? Correct spelling improves professionalism and clarity.",
                    word, correction)

    # Check 2: Repeated consecutive words
    # Catches typos like "the the" or "is is".
    for m in re.finditer(r'\b(\w+)\s+\1\b', text, re.IGNORECASE):
        add("Grammar", "error",
            f"Repeated word: '{m.group()}'",
            "The same word appears twice in a row — this is likely a typo.",
            m.group())

    # Check 3: Extra spaces
    # Two or more consecutive spaces can break layout and confuse screen readers.
    if re.search(r'  +', text):
        add("Grammar", "info",
            "Multiple consecutive spaces found",
            "Extra spaces can break formatting and confuse screen readers. "
            "Use a single space between words.",
            "Multiple consecutive spaces detected in the document")

    # Check 4: Per-sentence length
    # Long sentences are hard to parse, especially for users with cognitive
    # disabilities or those reading via a screen reader at speed.
    for s in sentences:
        wc   = _word_count(s)
        snip = (s[:110] + "…") if len(s) > 110 else s
        if wc > 35:
            add("Readability", "error",
                f"Very long sentence ({wc} words)",
                "Sentences over 35 words are very hard to follow. "
                "Try splitting into 2–3 shorter sentences.",
                snip)
        elif wc > 25:
            add("Readability", "warning",
                f"Long sentence ({wc} words)",
                "Aim for sentences under 25 words. Shorter sentences are easier for all readers, "
                "including those using screen readers.",
                snip)

    # Check 5: Flesch Reading Ease & Kincaid Grade
    # Flesch Reading Ease: 0–100 scale. Higher = easier. 60+ is the target for
    # general audiences. Below 30 is considered college-level or above.
    # We skip this for very short texts because the formula isn't reliable
    # with fewer than ~30 words.
    if total_words >= 30:
        fre  = textstat.flesch_reading_ease(text)
        fkgl = textstat.flesch_kincaid_grade(text)

        if fre < 30:
            add("Readability", "error",
                f"Very difficult to read (Flesch score {fre:.1f}/100)",
                "Below 30 is college-level and above. For general audiences, aim for 60+. "
                "Try using shorter sentences and simpler words.",
                f"Flesch Reading Ease score: {fre:.1f} / 100")
        elif fre < 50:
            add("Readability", "warning",
                f"Difficult to read (Flesch score {fre:.1f}/100)",
                "A score of 30–50 is considered 'difficult'. Simplify vocabulary and shorten sentences.",
                f"Flesch Reading Ease score: {fre:.1f} / 100")

        if fkgl > 12:
            add("Readability", "warning",
                f"College reading level detected (grade {fkgl:.1f})",
                "For broad audiences—including people with cognitive disabilities—target grade 8 or below.",
                f"Flesch-Kincaid Grade Level: {fkgl:.1f}")

    # Check 6: Passive voice overuse
    # A couple of passive sentences is fine; more than 4 in a document suggests
    # a habitual pattern that makes text harder to follow.
    passive_hits = _PASSIVE_RE.findall(text)
    if len(passive_hits) > 4:
        examples = "; ".join(passive_hits[:3])
        add("Readability", "info",
            f"Heavy use of passive voice ({len(passive_hits)} instances)",
            "Active voice is clearer and more direct. "
            "E.g., 'Errors were found by the team' → 'The team found errors'.",
            f"Examples: {examples}")

    # Check 7: Inclusive language
    # Iterate over every rule in _INCLUSIVE_RULES and scan the full text.
    # For each match we grab ~50 characters of surrounding context so the
    # snippet gives enough information to understand where the phrase appears.
    for pattern, suggestion in _INCLUSIVE_RULES:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            s0  = max(0, m.start() - 50)
            e0  = min(len(text), m.end() + 50)
            ctx = ("…" if s0 > 0 else "") + text[s0:e0].strip() + ("…" if e0 < len(text) else "")
            add("Accessibility", "warning",
                f"Non-inclusive language: '{m.group()}'",
                f"Consider '{suggestion}' instead. Inclusive language ensures all readers feel "
                "respected and represented.",
                ctx, suggestion)

    # Check 8: Excessive ALL CAPS
    # ALL CAPS words that have been formally defined in parentheses (e.g. "HTML"
    # from "(HTML)") are excluded — we only flag undefined ones.
    # More than 3 undeclared all-caps words suggests overuse.
    defined_acr  = set(re.findall(r'\(([A-Z]{2,7})\)', text))   # e.g. finds "HTML" from "(HTML)"
    all_caps     = re.findall(r'\b[A-Z]{4,}\b', text)
    caps_non_acr = [w for w in all_caps if w not in defined_acr]
    unique_caps  = list(dict.fromkeys(caps_non_acr))             # deduplicate, preserve order

    if len(unique_caps) > 3:
        add("Accessibility", "info",
            f"Excessive ALL CAPS text ({len(unique_caps)} instances)",
            "All-caps text is harder to read and can feel aggressive. "
            "Screen readers may read each letter individually. Use it sparingly.",
            "Examples: " + ", ".join(unique_caps[:7]))

    # Check 9: Undefined acronyms
    # Any capitalized sequence that never appears in parentheses elsewhere in the
    # document is considered 'undefined'. We skip a small allow-list of universal
    # abbreviations that virtually every reader will know.
    all_acr   = list(dict.fromkeys(_ACRONYM_RE.findall(text)))
    undefined = [a for a in all_acr if a not in defined_acr]
    skip_acr  = {"I", "A", "OK", "US", "UK", "UN", "AM", "PM", "AI", "IT"}
    undefined = [a for a in undefined if a not in skip_acr]

    if undefined:
        add("Accessibility", "info",
            f"Possibly undefined acronyms: {', '.join(undefined[:6])}",
            "Always spell out acronyms on first use, e.g. "
            "'Web Content Accessibility Guidelines (WCAG)'. "
            "Screen reader users and non-specialist readers may not recognise them.",
            ", ".join(undefined[:8]))

    # Check 10: Missing headings in long documents
    # We look for Markdown-style headings (# Heading) or plain-text headings
    # (a line that is Title Case or ALL CAPS on its own). If neither is found
    # in a document longer than 200 words, navigation is very hard for screen
    # reader users who rely on heading landmarks to jump around a page.
    has_md_headings        = bool(re.search(r'^#{1,6}\s+\w+', text, re.MULTILINE))
    has_plaintext_headings = bool(re.search(r'^[A-Z][A-Za-z ]{3,50}$', text, re.MULTILINE))

    if total_words > 200 and not has_md_headings and not has_plaintext_headings:
        add("Accessibility", "warning",
            "No headings detected in a long document",
            "Documents over 200 words should use headings so screen reader users can navigate. "
            "In Markdown, use # Heading, ## Sub-heading, etc.",
            f"Document has {total_words} words with no headings detected")

    # Check 11: Wall of text (no paragraph breaks)
    # A document that is one giant block of text is hard to scan for anyone,
    # and especially difficult for users with dyslexia or attention difficulties.
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) == 1 and total_words > 150:
        add("Readability", "info",
            "Text appears as a single block (no paragraph breaks)",
            "Breaking text into shorter paragraphs (every 3–5 sentences) improves readability "
            "and accessibility.",
            f"Entire document is one paragraph ({total_words} words)")

    # Check 12: Bare URLs as link text
    # A raw URL like https://example.com/very/long/path is read character-by-
    # character by screen readers, which is tedious and confusing. Descriptive
    # link text like [Read our guide](https://…) is far better.
    bare_urls = re.findall(r'https?://[^\s]+', text)
    if bare_urls:
        add("Accessibility", "warning",
            f"Bare URL(s) used as link text ({len(bare_urls)} found)",
            "Screen readers read URLs aloud character by character. Replace bare URLs with "
            "descriptive link text like [Read our accessibility guide](https://…) in Markdown.",
            bare_urls[0][:80])

    return issues


# ════════════════════════════════════════════════════════════════════════════════
# FILE EXTRACTION
# ════════════════════════════════════════════════════════════════════════════════

def extract_text(f) -> Optional[str]:
    """
    Read an uploaded Streamlit file object and return its plain-text contents.

    Supports .txt (always), .docx (needs python-docx), and .pdf (needs pdfplumber).
    Returns None and shows a warning if the required package isn't installed,
    or an error if the file itself can't be read.
    """
    name = f.name.lower()
    try:
        if name.endswith(".txt"):
            # Plain text — just decode the bytes directly
            return f.read().decode("utf-8", errors="replace")

        if name.endswith(".docx"):
            if HAS_DOCX:
                # python-docx needs a file-like object, so we wrap the raw bytes
                doc = _docx.Document(io.BytesIO(f.read()))
                # Join each paragraph with a blank line so structure is preserved
                return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            else:
                st.warning("python-docx not installed — cannot read .docx files. "
                           "Run: pip install python-docx")
                return None

        if name.endswith(".pdf"):
            if HAS_PDF:
                with _pdfplumber.open(io.BytesIO(f.read())) as pdf:
                    # Extract text page by page; some pages may return None so we default to ""
                    return "\n\n".join(pg.extract_text() or "" for pg in pdf.pages)
            else:
                st.warning("pdfplumber not installed — cannot read .pdf files. "
                           "Run: pip install pdfplumber")
                return None

        # Last resort: try to decode as UTF-8 regardless of file extension
        return f.read().decode("utf-8", errors="replace")

    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════════
# REPORT RENDERING
# ════════════════════════════════════════════════════════════════════════════════

# Emoji icons shown in tab headers and next to category badge text
_CAT_ICON = {"Grammar": "📝", "Readability": "📖", "Accessibility": "♿"}


def render_issue(issue: Issue, tab_prefix: str = ""):
    """
    Render a single issue as a colored header row + collapsible detail panel.

    The `tab_prefix` argument is critical: Streamlit renders ALL tab contents
    at once in the DOM, so if the same issue appears in both its category tab
    AND the 'All Issues' tab, its buttons would have duplicate keys and crash.
    Prefixing with something like "g_" (grammar) or "all_" makes every widget
    key globally unique, while the shared state keys (acc_/dis_) stay the same
    so that dismissing in one tab is reflected everywhere.
    """
    # wk  = widget key  — must be globally unique across all tabs
    # stk = state key   — shared, so accept/dismiss syncs across tabs
    wk  = f"{tab_prefix}{issue.id}"
    stk = str(issue.id)

    # If the user already dismissed this issue, skip rendering it entirely
    if st.session_state.get(f"dis_{stk}"):
        return

    # ── Colored header strip ─────────────────────────────────────────────────
    # We use raw HTML here because Streamlit's native components don't support
    # left-border coloring or inline badge pills.
    st.markdown(
        f'<div class="issue-row {issue.severity}">'
        f'<span class="badge b-{issue.severity}">{issue.severity.upper()}</span>'
        f'<span class="badge b-cat">{_CAT_ICON.get(issue.category, "")} {issue.category}</span>'
        f'<span style="font-weight:600">{issue.title}</span>'
        f'</div>',
        unsafe_allow_html=True
    )

    # ── Expandable detail panel ───────────────────────────────────────────────
    with st.expander("Details & Fix", expanded=False):

        # Plain English explanation of why this is a problem
        st.markdown(f"**What's wrong:** {issue.explanation}")

        # The actual text excerpt that triggered the issue
        st.markdown(
            f'<div class="snippet">{issue.snippet}</div>',
            unsafe_allow_html=True
        )

        # If a quick (non-AI) suggestion exists, show it as a blue info box
        if issue.suggestion:
            st.info(f"💡 Quick suggestion: **{issue.suggestion}**")

        col_fix, col_dis = st.columns([3, 1])

        with col_fix:
            # Check whether a fix has already been accepted for this issue
            accepted = st.session_state.get(f"acc_{stk}")
            if accepted:
                # Show the previously accepted fix; persists until re-analysis
                st.success(f"Fix noted: _{accepted}_")
            else:
                # AI fix placeholder — this caption will be replaced with the
                # "Get AI Fix" button when AI integration is added back.
                st.caption("AI Fix coming soon — use the quick suggestion above for now.")

        # ── Dismiss button ────────────────────────────────────────────────────
        # Sets a flag in session state. The issue disappears from all tabs on
        # the next rerun, and the 'Remaining' counter updates automatically.
        with col_dis:
            if not accepted:
                if st.button("✖ Dismiss", key=f"dis_btn_{wk}"):
                    st.session_state[f"dis_{stk}"] = True
                    st.rerun()


def render_report(issues: List[Issue], text: str):
    """
    Render the full interactive report: summary stats, filters, tabbed issue
    list, and export buttons.

    Receives the list of Issue objects from run_checks() and the original
    document text (needed for the download button).
    """

    # Happy path — no problems found
    if not issues:
        st.success("No accessibility issues found! Your document looks great.")
        return

    # ── Summary stat boxes ────────────────────────────────────────────────────
    # Count up each severity level and how many are still open vs dismissed
    errors    = sum(1 for i in issues if i.severity == "error")
    warnings  = sum(1 for i in issues if i.severity == "warning")
    infos     = sum(1 for i in issues if i.severity == "info")
    accepted  = sum(1 for i in issues if st.session_state.get(f"acc_{i.id}"))
    remaining = sum(1 for i in issues if not st.session_state.get(f"dis_{i.id}"))

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(
        f'<div class="stat-box" style="background:#fff5f5;color:#C62828">'
        f'<span class="stat-num">{errors}</span>Errors</div>',
        unsafe_allow_html=True)
    c2.markdown(
        f'<div class="stat-box" style="background:#fffbf0;color:#E65100">'
        f'<span class="stat-num">{warnings}</span>Warnings</div>',
        unsafe_allow_html=True)
    c3.markdown(
        f'<div class="stat-box" style="background:#f0f6ff;color:#1565C0">'
        f'<span class="stat-num">{infos}</span>Suggestions</div>',
        unsafe_allow_html=True)
    c4.markdown(
        f'<div class="stat-box" style="background:#f5fff5;color:#2E7D32">'
        f'<span class="stat-num">{accepted}</span>Noted</div>',
        unsafe_allow_html=True)
    c5.markdown(
        f'<div class="stat-box" style="background:#f5f5f5;color:#333">'
        f'<span class="stat-num">{remaining}</span>Remaining</div>',
        unsafe_allow_html=True)

    st.markdown("---")

    # ── Filter panel ──────────────────────────────────────────────────────────
    # Collapsed by default so it doesn't take up space unless the user needs it
    with st.expander("🔍 Filter issues", expanded=False):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            show_cats = st.multiselect(
                "Category",
                ["Grammar", "Readability", "Accessibility"],
                default=["Grammar", "Readability", "Accessibility"],
                key="filter_cat"
            )
        with fc2:
            show_sevs = st.multiselect(
                "Severity",
                ["error", "warning", "info"],
                default=["error", "warning", "info"],
                key="filter_sev"
            )
        with fc3:
            search_q = st.text_input("Search", placeholder="Filter by keyword…", key="filter_q")

    def apply_filters(issue_list):
        """Return only issues that pass the current category, severity, and search filters."""
        return [
            i for i in issue_list
            if i.category in show_cats
            and i.severity in show_sevs
            and not st.session_state.get(f"dis_{i.id}")   # hide dismissed issues
            and (
                not search_q
                or search_q.lower() in i.title.lower()
                or search_q.lower() in i.explanation.lower()
                or search_q.lower() in i.snippet.lower()
            )
        ]

    # ── Tabbed issue list ─────────────────────────────────────────────────────
    # Each category gets its own tab so users can focus on one area at a time.
    # The 'All Issues' tab shows everything together for a full overview.
    #
    # IMPORTANT: Streamlit renders all tab bodies simultaneously in the DOM,
    # which is why render_issue() needs a tab_prefix for unique widget keys.
    tab_grammar, tab_read, tab_access, tab_all = st.tabs(
        ["📝 Grammar", "📖 Readability", "♿ Accessibility", "🔍 All Issues"]
    )

    with tab_grammar:
        filtered = apply_filters([i for i in issues if i.category == "Grammar"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Grammar issues match the current filters.")
        for issue in filtered:
            render_issue(issue, tab_prefix="g_")

    with tab_read:
        filtered = apply_filters([i for i in issues if i.category == "Readability"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Readability issues match the current filters.")
        for issue in filtered:
            render_issue(issue, tab_prefix="r_")

    with tab_access:
        filtered = apply_filters([i for i in issues if i.category == "Accessibility"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Accessibility issues match the current filters.")
        for issue in filtered:
            render_issue(issue, tab_prefix="a_")

    with tab_all:
        filtered = apply_filters(issues)
        st.markdown(f"**{len(filtered)} issue(s) shown**")
        if not filtered:
            st.info("No issues match the current filters.")
        for issue in filtered:
            render_issue(issue, tab_prefix="all_")

    # ── Export ────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📥 Export")
    ec1, ec2 = st.columns(2)

    with ec1:
        # Always available — re-encodes the original document text as UTF-8 bytes
        st.download_button(
            "⬇ Download original text (.txt)",
            data=text.encode("utf-8"),
            file_name="document_original.txt",
            mime="text/plain"
        )

    with ec2:
        # Build a plain-text summary of every issue (dismissed ones are labelled)
        report_lines = ["WRITEABLE – ISSUES REPORT", "=" * 45, ""]
        for iss in issues:
            status = "DISMISSED" if st.session_state.get(f"dis_{iss.id}") else "OPEN"
            report_lines += [
                f"[{iss.category.upper()} / {iss.severity.upper()}] — {status}",
                f"Issue   : {iss.title}",
                f"Snippet : {iss.snippet}",
                f"Tip     : {iss.suggestion or 'See explanation above'}",
                ""
            ]
        st.download_button(
            "⬇ Download issues report (.txt)",
            data="\n".join(report_lines).encode("utf-8"),
            file_name="issues_report.txt",
            mime="text/plain"
        )


# ════════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ════════════════════════════════════════════════════════════════════════════════

# Show the logo if it exists next to app.py, otherwise fall back to plain text
logo_path = Path("logo.png")
if logo_path.exists():
    st.sidebar.image(str(logo_path), width="stretch")
else:
    st.sidebar.markdown("## 📝 WriteAble")

st.sidebar.markdown("---")
st.sidebar.markdown("## Navigation")

# Simple two-section nav: the main working app vs the guides/about content
main_page = st.sidebar.radio(
    "Go to:",
    ["Main App", "Guides & About"],
    label_visibility="collapsed"
)

# Quick at-a-glance status so users can see which optional features are active
st.sidebar.markdown("---")
st.sidebar.markdown("**Package status**")
st.sidebar.markdown(f"{'✅' if HAS_SPELL else '⚠️'} Spell checker "
                    f"({'ready' if HAS_SPELL else 'pip install pyspellchecker'})")
st.sidebar.markdown(f"{'✅' if HAS_DOCX else '⚠️'} DOCX support "
                    f"({'ready' if HAS_DOCX else 'pip install python-docx'})")
st.sidebar.markdown(f"{'✅' if HAS_PDF else '⚠️'} PDF support "
                    f"({'ready' if HAS_PDF else 'pip install pdfplumber'})")


# ════════════════════════════════════════════════════════════════════════════════
# PAGES
# ════════════════════════════════════════════════════════════════════════════════

if main_page == "Main App":

    # ── Overview banner ───────────────────────────────────────────────────────
    st.title("WriteAble – Accessible Document Helper")
    st.markdown(
        "WriteAble analyzes documents for **accessibility, readability, and grammar issues** "
        "and provides plain-language explanations for every problem it finds."
    )

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Check types", "12", "Grammar, Readability, Accessibility")
    col_b.metric("Max file size", "200 MB", "PDF, DOCX, TXT")
    col_c.metric("Status", "Live ✅", "All checks active")

    st.markdown("---")

    # ── Upload / paste section ────────────────────────────────────────────────
    st.subheader("Step 1 : Upload or paste your document")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Upload a file**")
        uploaded = st.file_uploader(
            "Choose a file (TXT, DOCX, or PDF)",
            type=["txt", "docx", "pdf"],
            help="Files are processed in memory and never stored anywhere."
        )
        if uploaded:
            st.info(f"File loaded: **{uploaded.name}** ({uploaded.size / 1024:.1f} KB)")

    with col2:
        st.markdown("**Or paste text directly**")
        pasted = st.text_area(
            "Paste your document text here:",
            height=220,
            placeholder="Paste any document text here…",
            help="Plain text, Markdown, or a copy-paste from any document."
        )

    st.markdown("---")

    # ── Run button ────────────────────────────────────────────────────────────
    st.subheader("Step 2 : Run the accessibility check")

    if st.button("Run Accessibility Check", type="primary"):

        # Figure out which input source to use — uploaded file takes priority
        text         = None
        source_label = ""

        if uploaded:
            with st.spinner("Reading file…"):
                text         = extract_text(uploaded)
                source_label = uploaded.name
        elif pasted and pasted.strip():
            text         = pasted.strip()
            source_label = "pasted text"
        else:
            st.warning("Please upload a file or paste some text first.")

        if text and text.strip():
            if len(text.strip()) < 20:
                st.warning("Text is too short to analyse (need at least 20 characters).")
            else:
                with st.spinner("Running all 12 accessibility checks…"):
                    issues = run_checks(text)

                # Store results in session state so they survive Streamlit reruns.
                # Streamlit re-executes the whole script on every user interaction,
                # so anything we want to keep between clicks must live in session_state.
                st.session_state["analysis_text"]   = text
                st.session_state["analysis_issues"] = issues
                st.session_state["analysis_source"] = source_label

                # Wipe any dismiss/accept flags left over from a previous analysis
                for key in [k for k in st.session_state
                            if k.startswith(("fix_", "acc_", "dis_"))]:
                    del st.session_state[key]

                st.success(
                    f"✅ Analysis complete — **{len(issues)} issue(s)** found in {source_label}"
                )

                # Mini preview so the user gets instant feedback before scrolling down
                errors   = sum(1 for i in issues if i.severity == "error")
                warnings = sum(1 for i in issues if i.severity == "warning")
                infos    = sum(1 for i in issues if i.severity == "info")
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("🔴 Errors",      errors)
                pc2.metric("🟡 Warnings",    warnings)
                pc3.metric("🔵 Suggestions", infos)

    st.markdown("---")

    # ── Results section ───────────────────────────────────────────────────────
    st.subheader("Step 3 : Review the interactive report")

    if "analysis_issues" not in st.session_state:
        # Nothing has been analysed yet — show a gentle prompt
        st.info("Run a check above to see results here.")
    else:
        issues = st.session_state["analysis_issues"]
        text   = st.session_state["analysis_text"]
        source = st.session_state.get("analysis_source", "document")

        st.markdown(
            f"**Source:** {source} &nbsp;|&nbsp; "
            f"**{len(text.split())} words** &nbsp;|&nbsp; "
            f"**{len(issues)} issue(s) found**"
        )
        st.markdown("---")

        # Hand off to the report renderer — all the interactive UI lives there
        render_report(issues, text)


# ── Guides & About ────────────────────────────────────────────────────────────
elif main_page == "Guides & About":

    tab1, tab2, tab3 = st.tabs(["⚡ Quick Guide", "📘 Full Guide", "ℹ️ About"])

    with tab1:
        st.title("Quick User Guide")
        st.markdown("""
        **1. Upload or paste**
        Go to the *Main App* page. Upload a TXT, DOCX, or PDF file — or paste text directly
        into the text box.

        **2. Run the check**
        Click **Run Accessibility Check**. WriteAble will run 12 checks across grammar,
        readability, and accessibility.

        **3. Read the summary**
        A row of stat boxes shows how many Errors, Warnings, and Suggestions were found.

        **4. Browse by category**
        Issues are split into three tabs — Grammar, Readability, Accessibility — plus an
        All Issues view. Click any coloured row to expand it.

        **5. Read the explanation**
        Each issue includes a plain-English description of *why* it matters, the offending
        text snippet, and a quick suggestion where one is available.

        **6. Dismiss issues**
        If an issue isn't relevant, click **✖ Dismiss** to hide it. The Remaining counter
        updates automatically.

        **7. Export**
        Scroll to the bottom of the report to download your original document or a full
        issues report as a `.txt` file.
        """)

    with tab2:
        st.title("Full User Guide")
        st.markdown("""
        ### Supported Formats

        | Format | Support |
        |--------|---------|
        | Plain text (.txt) | ✅ Always available |
        | Word document (.docx) | ✅ Requires `python-docx` |
        | PDF (.pdf) | ✅ Requires `pdfplumber` |
        | Pasted text | ✅ Always available |

        Maximum recommended file size: **200 MB**.

        ---

        ### What Each Check Does

        **Grammar**
        - *Spelling* — Flags potentially misspelled words and suggests corrections.
          Capitalised words are skipped to avoid flagging proper nouns.
        - *Repeated words* — Detects accidental double words like "the the".
        - *Extra spaces* — Flags two or more consecutive spaces.

        **Readability**
        - *Sentence length* — Flags sentences over 25 words (warning) or 35 words (error).
        - *Flesch Reading Ease* — A 0–100 score; 60+ suits most general audiences.
        - *Flesch-Kincaid Grade Level* — US grade equivalent; aim for Grade 8 or below.
        - *Passive voice* — Flags documents with more than 4 passive constructions.

        **Accessibility**
        - *Inclusive language* — Checks 21 non-inclusive patterns and suggests alternatives.
        - *ALL CAPS overuse* — Flags more than 3 undeclared all-caps words.
        - *Undefined acronyms* — Flags acronyms that are never spelled out in full.
        - *Missing headings* — Warns when a document over 200 words has no headings.
        - *Wall of text* — Warns when the entire document is one unbroken paragraph.
        - *Bare URLs* — Flags raw links that screen readers would read aloud letter by letter.

        ---

        ### Exporting

        - **Download original text** — Your source document as plain text.
        - **Download issues report** — A structured list of every issue found, including its
          category, severity, snippet, and quick tip. Dismissed issues are marked as such.

        ---

        ### Accessibility of WriteAble Itself

        - High-contrast badges and colour-coded severity levels
        - Keyboard-navigable interface (Streamlit's built-in support)
        - Plain-language explanations for every issue type
        - No animations, auto-play media, or flashing content
        """)

    with tab3:
        st.title("About WriteAble")
        st.markdown("""
        WriteAble helps writers, content creators, and teams produce documents that are clearer,
        more inclusive, and accessible to all readers — including people who use assistive
        technology such as screen readers.

        **Our principles:**
        - Accessibility checks should be *understandable*, not just flagged
        - Plain-language explanations help writers learn, not just fix
        - Every suggestion should teach something, not just issue a correction

        **Technology stack:**
        - [Streamlit](https://streamlit.io) — UI framework
        - [textstat](https://github.com/textstat/textstat) — Readability metrics
        - [pyspellchecker](https://github.com/barrust/pyspellchecker) — Spell checking

        **Standards alignment:**
        - Inclusive language rules align with disability-first language guidance
        - Reading level targets follow [Plain Language Guidelines](https://www.plainlanguage.gov/)
        - Structural checks are informed by [WCAG 2.1](https://www.w3.org/TR/WCAG21/)
        """)