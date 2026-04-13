"""
WriteAble – Accessible Document Helper
Real accessibility checking + interactive AI-powered fix report.
"""
import io
import re
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

import streamlit as st
import textstat

from openai import OpenAI
import json

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from spellchecker import SpellChecker
    _spell = SpellChecker()
    HAS_SPELL = True
except Exception:
    HAS_SPELL = False

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except Exception:
    HAS_ANTHROPIC = False

try:
    import docx as _docx
    HAS_DOCX = True
except Exception:
    HAS_DOCX = False

try:
    import pdfplumber as _pdfplumber
    HAS_PDF = True
except Exception:
    HAS_PDF = False


# ════════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & CSS
# ════════════════════════════════════════════════════════════════════════════════
# Set up Streamlit page settings
st.set_page_config(page_title="WriteAble – Accessible Document Helper",
                   page_icon="📝", layout="wide")

st.markdown("""
<style>
html, body, [class*="css"] { font-size: 16px; }
h1, h2, h3 { font-weight: 700; }

.stButton > button {
    background: #005A9E; color: white;
    border: 2px solid #003B6F; border-radius: 4px;
    font-size: 14px;
}
.stButton > button:hover { background: #0078D4; border-color: #005A9E; }

/* Issue row */
.issue-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-radius: 6px; margin: 5px 0;
    border-left: 5px solid #ccc; background: #fafafa;
}
.issue-row.error   { border-color: #C62828; background: #fff5f5; }
.issue-row.warning { border-color: #E65100; background: #fffbf0; }
.issue-row.info    { border-color: #1565C0; background: #f0f6ff; }

/* Badges */
.badge {
    padding: 2px 9px; border-radius: 12px;
    font-size: 11px; font-weight: 800;
    color: white; white-space: nowrap; display: inline-block;
}
.b-error   { background: #C62828; }
.b-warning { background: #E65100; }
.b-info    { background: #1565C0; }
.b-cat     { background: #444; font-size: 11px; }

/* Code snippet */
.snippet {
    font-family: monospace; font-size: 13px;
    background: #eeeeee; color: #000000 !important; padding: 6px 10px;
    border-radius: 4px; margin: 8px 0; word-break: break-word;
    white-space: pre-wrap;
}

/* AI fix box */
.fix-box {
    background: #e8f5e9; border-left: 4px solid #2E7D32;
    padding: 10px 14px; border-radius: 4px; margin-top: 8px;
    font-size: 14px;
}

/* Summary stat boxes */
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
    id: int
    category: str   # Grammar | Readability | Accessibility
    severity: str   # error | warning | info
    title: str
    explanation: str
    snippet: str
    suggestion: str = ""   # quick pre-computed replacement (may be empty)


# ════════════════════════════════════════════════════════════════════════════════
# CHECKER ENGINE
# ════════════════════════════════════════════════════════════════════════════════
# Define rules and regex patterns used in text analysis
_INCLUSIVE_RULES = [
    (r"\bthe\s+disabled\b",               "people with disabilities"),
    (r"\bthe\s+blind\b",                  "people who are blind"),
    (r"\bthe\s+deaf\b",                   "people who are Deaf or hard of hearing"),
    (r"\bwheelchair[\s\-]?bound\b",       "wheelchair user"),
    (r"\bconfined\s+to\s+a\s+wheelchair\b","wheelchair user"),
    (r"\bsuffers?\s+from\b",              "has / lives with"),
    (r"\bmentally\s+ill\b",               "person with a mental health condition"),
    (r"\bcrippled?\b",                    "person with a disability"),
    (r"\bmankind\b",                      "humanity or humankind"),
    (r"\bmanpower\b",                     "workforce or staffing"),
    (r"\bblacklist\b",                    "blocklist or denylist"),
    (r"\bwhitelist\b",                    "allowlist"),
    (r"\bhe\s+or\s+she\b",               "they"),
    (r"\bhis\s+or\s+her\b",              "their"),
    (r"\bcrazy\b",                        "unexpected / surprising"),
    (r"\binsane\b",                       "extreme / unreasonable"),
    (r"\bdumb\b",                         "confusing or unclear"),
    (r"\bstupid\b",                       "unclear / poorly designed"),
    (r"\blow[\s\-]functioning\b",         "person who needs significant support"),
    (r"\bhigh[\s\-]functioning\b",        "person who needs minimal support"),
    (r"\bnormal\s+people\b",             "people without disabilities"),
]

_PASSIVE_RE = re.compile(
    r'\b(?:am|is|are|was|were|be|been|being)\s+\w+(?:ed|en)\b',
    re.IGNORECASE
)
_ACRONYM_RE = re.compile(r'\b([A-Z]{2,7})\b')

# Helper function to count words in text
def _word_count(text: str) -> int:
    return len(re.findall(r'\b[a-zA-Z]+\b', text))


# Helper function to split text into sentences
def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

# Handles page navigation using query parameters
def go_to(page_name: str):
    st.query_params["page"] = page_name

# Main function that runs all accessibility and readability checks
def run_checks(text: str) -> List[Issue]:
    issues: List[Issue] = []
    _id = 0

    # Helper function to add issues to the list
    def add(cat, sev, title, expl, snip, sug=""):
        nonlocal _id
        _id += 1
        issues.append(Issue(_id, cat, sev, title, expl, snip, sug))

    # Preprocess text into words, sentences, and counts
    all_words = re.findall(r'\b[a-zA-Z]+\b', text)
    sentences  = _sentences(text)
    total_words = _word_count(text)

    # Check for spelling errors using spellchecker
    if HAS_SPELL:
        # Only check lowercase, length > 3 to avoid proper nouns & abbreviations
        check_words = [w for w in all_words if not w[0].isupper() and len(w) > 3]
        misspelled  = _spell.unknown(check_words)
        for word in sorted(misspelled)[:12]:
            correction = _spell.correction(word)
            if correction and correction != word:
                add("Grammar", "error",
                    f"Possible misspelling: '{word}'",
                    f"Did you mean '{correction}'? Correct spelling improves professionalism and clarity.",
                    word, correction)

    # Detect repeated words
    for m in re.finditer(r'\b(\w+)\s+\1\b', text, re.IGNORECASE):
        add("Grammar", "error",
            f"Repeated word: '{m.group()}'",
            "The same word appears twice in a row — this is likely a typo.",
            m.group())

    # Detect extra spaces in the text
    if re.search(r'  +', text):
        add("Grammar", "info",
            "Multiple consecutive spaces found",
            "Extra spaces can break formatting and confuse screen readers. Use a single space between words.",
            "Multiple consecutive spaces detected in the document")

    # Check sentence length for readability issues
    for s in sentences:
        wc   = _word_count(s)
        snip = (s[:110] + "…") if len(s) > 110 else s
        if wc > 35:
            add("Readability", "error",
                f"Very long sentence ({wc} words)",
                "Sentences over 35 words are very hard to follow. Try splitting into 2–3 shorter sentences.",
                snip)
        elif wc > 25:
            add("Readability", "warning",
                f"Long sentence ({wc} words)",
                "Aim for sentences under 25 words. Shorter sentences are easier for all readers, including those using screen readers.",
                snip)

    # Calculate readability scores (Flesch and grade level)
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

    # Detect overuse of passive voice
    passive_hits = _PASSIVE_RE.findall(text)
    if len(passive_hits) > 4:
        examples = "; ".join(passive_hits[:3])
        add("Readability", "info",
            f"Heavy use of passive voice ({len(passive_hits)} instances)",
            "Active voice is clearer and more direct. E.g., 'Errors were found by the team' → 'The team found errors'.",
            f"Examples: {examples}")

    # Check for non-inclusive language patterns
    for pattern, suggestion in _INCLUSIVE_RULES:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            s0  = max(0, m.start() - 50)
            e0  = min(len(text), m.end() + 50)
            ctx = ("…" if s0 > 0 else "") + text[s0:e0].strip() + ("…" if e0 < len(text) else "")
            add("Accessibility", "warning",
                f"Non-inclusive language: '{m.group()}'",
                f"Consider '{suggestion}' instead. Inclusive language ensures all readers feel respected and represented.",
                ctx, suggestion)

    # Detect excessive use of ALL CAPS text
    defined_acr = set(re.findall(r'\(([A-Z]{2,7})\)', text))
    all_caps    = re.findall(r'\b[A-Z]{4,}\b', text)
    # Exclude defined acronyms
    caps_non_acr = [w for w in all_caps if w not in defined_acr]
    unique_caps  = list(dict.fromkeys(caps_non_acr))
    if len(unique_caps) > 3:
        add("Accessibility", "info",
            f"Excessive ALL CAPS text ({len(unique_caps)} instances)",
            "All-caps text is harder to read and can feel aggressive. "
            "Screen readers may read each letter individually. Use it sparingly.",
            "Examples: " + ", ".join(unique_caps[:7]))

    # Detect acronyms that are not defined
    all_acr   = list(dict.fromkeys(_ACRONYM_RE.findall(text)))
    undefined = [a for a in all_acr if a not in defined_acr]
    # Filter very common ones people wouldn't need to define
    skip_acr  = {"I", "A", "OK", "US", "UK", "UN", "AM", "PM", "AI", "IT"}
    undefined = [a for a in undefined if a not in skip_acr]
    if undefined:
        add("Accessibility", "info",
            f"Possibly undefined acronyms: {', '.join(undefined[:6])}",
            "Always spell out acronyms on first use, e.g. 'Web Content Accessibility Guidelines (WCAG)'. "
            "Screen reader users and non-specialist readers may not recognise them.",
            ", ".join(undefined[:8]))

    # Check if long documents are missing headings
    has_md_headings = bool(re.search(r'^#{1,6}\s+\w+', text, re.MULTILINE))
    # Also detect plain-text heading style (line of text alone on a line, all caps or Title Case)
    has_plaintext_headings = bool(re.search(r'^[A-Z][A-Za-z ]{3,50}$', text, re.MULTILINE))
    if total_words > 200 and not has_md_headings and not has_plaintext_headings:
        add("Accessibility", "warning",
            "No headings detected in a long document",
            "Documents over 200 words should use headings so screen reader users can navigate. "
            "In Markdown, use # Heading, ## Sub-heading, etc.",
            f"Document has {total_words} words with no headings detected")

    # Detect large blocks of text without paragraph breaks
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) == 1 and total_words > 150:
        add("Readability", "info",
            "Text appears as a single block (no paragraph breaks)",
            "Breaking text into shorter paragraphs (every 3–5 sentences) improves readability and accessibility.",
            f"Entire document is one paragraph ({total_words} words)")

    # Detect raw URLs instead of descriptive links
    bare_urls = re.findall(r'https?://[^\s]+', text)
    if bare_urls:
        add("Accessibility", "warning",
            f"Bare URL(s) used as link text ({len(bare_urls)} found)",
            "Screen readers read URLs aloud character by character. Replace bare URLs with descriptive link text like "
            "[Read our accessibility guide](https://…) in Markdown.",
            bare_urls[0][:80])

    # Return all detected issues
    return issues


# ════════════════════════════════════════════════════════════════════════════════
# AI FIX
# ════════════════════════════════════════════════════════════════════════════════
def get_ai_fix_openai(issue: Issue, full_text: str) -> str:
    prompt = f"""
    You are an expert in accessibility, readability, and grammar.

    Issue category: {issue.category}
    Issue: {issue.title}
    Explanation: {issue.explanation}
    Problematic text: {issue.snippet}

    Return JSON with:
    - suggestions (list)
    - each suggestion must include:
        - label
        - explanation
        - updated_text

    Only return valid JSON.
    """

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content


# ════════════════════════════════════════════════════════════════════════════════
# FILE EXTRACTION
# ════════════════════════════════════════════════════════════════════════════════
def extract_text(f) -> Optional[str]:
    name = f.name.lower()
    try:
        # Handle plain text files
        if name.endswith(".txt"):
            return f.read().decode("utf-8", errors="replace")
        # Handle Word documents if library is installed
        if name.endswith(".docx"):
            if HAS_DOCX:
                doc = _docx.Document(io.BytesIO(f.read()))
                return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            else:
                st.warning("python-docx not installed. Install it to support .docx files.")
                return None
        # Handle PDFs if library is installed
        if name.endswith(".pdf"):
            if HAS_PDF:
                with _pdfplumber.open(io.BytesIO(f.read())) as pdf:
                    return "\n\n".join(pg.extract_text() or "" for pg in pdf.pages)
            else:
                st.warning("pdfplumber not installed. Install it to support .pdf files.")
                return None
        # Fallback
        return f.read().decode("utf-8", errors="replace")
    # Catch and display any file reading errors
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════════
# REPORT RENDERING
# ════════════════════════════════════════════════════════════════════════════════
# Define colors and icons used when rendering issues in the interface
_SEV_COLOR = {"error": "#C62828",  "warning": "#E65100", "info": "#1565C0"}
_BG_COLOR  = {"error": "#fff5f5",  "warning": "#fffbf0", "info": "#f0f6ff"}
_CAT_ICON  = {"Grammar": "📝",     "Readability": "📖",  "Accessibility": "♿"}

# Render a single issue card in the UI with details and actions
def render_issue(issue: Issue, full_text: str, api_key: Optional[str], tab_prefix: str = ""):
    # wk  = widget key  (must be unique across ALL tabs rendered simultaneously)
    # stk = state key   (shared across tabs so accept/dismiss is consistent everywhere)
    wk  = f"{tab_prefix}{issue.id}"
    stk = str(issue.id)

    # Skip if dismissed
    if st.session_state.get(f"dis_{stk}"):
        return

    # Display issue header (severity, category, title)
    st.markdown(
        f'<div class="issue-row {issue.severity}">'
        f'<span class="badge b-{issue.severity}">{issue.severity.upper()}</span>'
        f'<span class="badge b-cat">{_CAT_ICON.get(issue.category,"")} {issue.category}</span>'
        f'<span style="font-weight:600">{issue.title}</span>'
        f'</div>',
        unsafe_allow_html=True
    )
    
    # Expandable section for full details and fixes
    with st.expander("Details & Fix", expanded=False):
        st.markdown(f"**What's wrong:** {issue.explanation}")
        st.markdown(
            f'<div class="snippet">{issue.snippet}</div>',
            unsafe_allow_html=True
        )

        # Show quick suggestion if available
        if issue.suggestion:
            st.info(f"💡 Quick suggestion: **{issue.suggestion}**")

        # Layout for fix and dismiss buttons
        col_fix, col_dis = st.columns([3, 1])
        with col_fix:
            accepted = st.session_state.get(f"acc_{stk}")
            ai_fix   = st.session_state.get(f"fix_{stk}")

            # If user already accepted a fix
            if accepted:
                st.success(f"✅ Fix accepted: _{accepted}_")
            # If AI fix exists but not accepted yet
            elif ai_fix:
                st.markdown('<div class="fix-box">🤖 <b>AI suggestion:</b></div>', unsafe_allow_html=True)

                try:
                    parsed = json.loads(ai_fix)

                    for sug in parsed["suggestions"]:
                        st.markdown(f"**Fix:** {sug['label']}")
                        st.markdown(f"_{sug['explanation']}_")
                        st.code(sug["updated_text"])

                except:
                    st.write(ai_fix)
                # Button to accept AI fix
                if st.button("✅ Accept this fix", key=f"acc_btn_{wk}"):
                    st.session_state[f"acc_{stk}"] = ai_fix
                    st.rerun()
            # If no AI fix yet, allow generating one
            else:
                if st.button("🤖 Get AI Fix", key=f"ai_btn_{wk}"):
                    with st.spinner("Generating AI fix…"):
                        raw = get_ai_fix_openai(issue, full_text)

                        try:
                            parsed = json.loads(raw)
                            suggestion = parsed["suggestions"][0]["updated_text"]
                        except:
                            suggestion = raw  # fallback

                        st.session_state[f"fix_{stk}"] = suggestion
                        st.rerun()

        # Dismiss button section
        with col_dis:
            if not accepted:
                if st.button("✖ Dismiss", key=f"dis_btn_{wk}"):
                    st.session_state[f"dis_{stk}"] = True
                    st.rerun()

# Render the full report
def render_report(issues: List[Issue], text: str, api_key: Optional[str]):
    """Full interactive report with summary, filters, tabs, and export."""

    # If no issues found, show success message
    if not issues:
        st.success("🎉 No accessibility issues found! Your document looks great.")
        return

    # Calculate summary statistics
    errors   = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")
    infos    = sum(1 for i in issues if i.severity == "info")
    active   = sum(1 for i in issues if not st.session_state.get(f"dis_{i.id}"))
    accepted = sum(1 for i in issues if st.session_state.get(f"acc_{i.id}"))

    # Display summary metrics in columns
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(
        f'<div class="stat-box" style="background:#fff5f5;color:#C62828">'
        f'<span class="stat-num">{errors}</span>Errors</div>', unsafe_allow_html=True)
    c2.markdown(
        f'<div class="stat-box" style="background:#fffbf0;color:#E65100">'
        f'<span class="stat-num">{warnings}</span>Warnings</div>', unsafe_allow_html=True)
    c3.markdown(
        f'<div class="stat-box" style="background:#f0f6ff;color:#1565C0">'
        f'<span class="stat-num">{infos}</span>Suggestions</div>', unsafe_allow_html=True)
    c4.markdown(
        f'<div class="stat-box" style="background:#f5fff5;color:#2E7D32">'
        f'<span class="stat-num">{accepted}</span>Fixes Accepted</div>', unsafe_allow_html=True)
    c5.markdown(
        f'<div class="stat-box" style="background:#f5f5f5;color:#333">'
        f'<span class="stat-num">{active}</span>Remaining</div>', unsafe_allow_html=True)

    st.markdown("---")

    # ── Filters ───────────────────────────────────────────────────────────────
    with st.expander("🔍 Filter issues", expanded=False):
        fc1, fc2, fc3 = st.columns(3)
        # Filter by category
        with fc1:
            show_cats = st.multiselect(
                "Category",
                ["Grammar", "Readability", "Accessibility"],
                default=["Grammar", "Readability", "Accessibility"],
                key="filter_cat"
            )
        # Filter by severity level
        with fc2:
            show_sevs = st.multiselect(
                "Severity",
                ["error", "warning", "info"],
                default=["error", "warning", "info"],
                key="filter_sev"
            )
        # Search box for keyword filtering
        with fc3:
            search_q = st.text_input("Search", placeholder="Filter by keyword…", key="filter_q")

    # Apply all selected filters to the issue list
    def apply_filters(issue_list):
        return [
            i for i in issue_list
            if i.category in show_cats
            and i.severity in show_sevs
            and not st.session_state.get(f"dis_{i.id}")
            and (not search_q
                 or search_q.lower() in i.title.lower()
                 or search_q.lower() in i.explanation.lower()
                 or search_q.lower() in i.snippet.lower())
        ]

    # Create tabs to organize issues by category
    tab_grammar, tab_read, tab_access, tab_all = st.tabs(
        ["📝 Grammar", "📖 Readability", "♿ Accessibility", "🔍 All Issues"]
    )

    # Display grammar issues
    with tab_grammar:
        filtered = apply_filters([i for i in issues if i.category == "Grammar"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Grammar issues match the current filters.")
        for issue in filtered:
            render_issue(issue, text, api_key, tab_prefix="g_")

    # Display readability issues
    with tab_read:
        filtered = apply_filters([i for i in issues if i.category == "Readability"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Readability issues match the current filters.")
        for issue in filtered:
            render_issue(issue, text, api_key, tab_prefix="r_")

    # Display accessibility issues
    with tab_access:
        filtered = apply_filters([i for i in issues if i.category == "Accessibility"])
        st.markdown(f"**{len(filtered)} issue(s)**")
        if not filtered:
            st.info("No Accessibility issues match the current filters.")
        for issue in filtered:
            render_issue(issue, text, api_key, tab_prefix="a_")

    # Display all issues together
    with tab_all:
        filtered = apply_filters(issues)
        st.markdown(f"**{len(filtered)} issue(s) shown**")
        if not filtered:
            st.info("No issues match the current filters.")
        for issue in filtered:
            render_issue(issue, text, api_key, tab_prefix="all_")

    # Export section for downloading results
    st.markdown("---")
    st.subheader("📥 Export")
    ec1, ec2 = st.columns(2)

    # Download original text
    with ec1:
        st.download_button(
            "⬇ Download original text (.txt)",
            data=text.encode("utf-8"),
            file_name="document_original.txt",
            mime="text/plain"
        )

    with ec2:
        # Build a fixes summary report
        accepted_issues = [(i, st.session_state.get(f"acc_{i.id}"))
                           for i in issues if st.session_state.get(f"acc_{i.id}")]
        if accepted_issues:
            report_lines = ["WRITEABLE – ACCEPTED FIXES REPORT", "=" * 45, ""]
            # Build report content line-by-line
            for iss, fix in accepted_issues:
                report_lines += [
                    f"[{iss.category.upper()} / {iss.severity.upper()}]",
                    f"Issue   : {iss.title}",
                    f"Original: {iss.snippet}",
                    f"Fix     : {fix}",
                    ""
                ]
            st.download_button(
                "⬇ Download fixes report (.txt)",
                data="\n".join(report_lines).encode("utf-8"),
                file_name="fixes_report.txt",
                mime="text/plain"
            )
        else:
            st.caption("Accept at least one AI fix to download a fixes report.")


# ════════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ════════════════════════════════════════════════════════════════════════════════
# Display sidebar with navigation, settings, and system status
logo_path = Path("logo.png")
# Show logo if it exists, otherwise show text title
if logo_path.exists():
    st.sidebar.markdown(
        f"""
        <a href="/" target="_self" style="text-decoration: none;">
            <img src="data:image/png;base64,{logo_path.read_bytes().encode('base64').decode()}" 
                 style="width:100%; border-radius:10px; margin-bottom:10px;">
        </a>
        """,
        unsafe_allow_html=True
    )
else:
    st.sidebar.markdown("## 📝 WriteAble")

# Navigation section for switching between pages
st.sidebar.markdown("---")
st.sidebar.markdown("## Navigation")
current_page = st.query_params.get("page", "main")
main_page = st.sidebar.radio(
    "Go to:",
    ["Main App", "Guides & About"],
    index=0 if current_page == "main" else 1,
    label_visibility="collapsed"
)

# Update URL query params based on selected page
if main_page == "Main App":
    st.query_params["page"] = "main"
else:
    st.query_params["page"] = "guides"

# Input field for API key used in AI fixes
st.sidebar.markdown("---")
st.sidebar.markdown("## 🤖 AI Fix Settings")
api_key = st.sidebar.text_input(
    "Anthropic API Key",
    type="password",
    help="Needed for AI Fix suggestions. Your key is never stored.",
    placeholder="sk-ant-…"
) or None

# Status indicators for feautures
st.sidebar.markdown("---")
st.sidebar.markdown("**Package status**")
st.sidebar.markdown(f"{'✅' if HAS_SPELL else '⚠'} Spell checker ({'pyspellchecker' if HAS_SPELL else 'not installed'})")
st.sidebar.markdown(f"{'✅' if HAS_ANTHROPIC else '⚠'} AI fixes ({'anthropic' if HAS_ANTHROPIC else 'not installed'})")
st.sidebar.markdown(f"{'✅' if HAS_DOCX else '⚠'} DOCX support ({'python-docx' if HAS_DOCX else 'not installed'})")
st.sidebar.markdown(f"{'✅' if HAS_PDF else '⚠'} PDF support ({'pdfplumber' if HAS_PDF else 'not installed'})")


# ════════════════════════════════════════════════════════════════════════════════
# PAGES
# ════════════════════════════════════════════════════════════════════════════════
# Main App Functionality
if main_page == "Main App":


    # Overview section
        st.title("WriteAble – Accessible Document Helper")
        st.markdown("""
        WriteAble analyzes documents for **accessibility, readability, and grammar issues**
        and provides plain-language explanations and AI-powered fix suggestions.
        """)

        # Display quick stats at top
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Check types", "12", "Grammar, Readability, Accessibility")
        col_b.metric("Max file size", "20 MB", "PDF, DOCX, TXT")
        col_c.metric("AI fix model", "Claude Haiku", "Fast & accurate")

        st.markdown("---")
        
    # Upload section
        st.title("Upload or Paste Your Document")

        col1, col2 = st.columns(2)

        # File upload input
        with col1:
            st.subheader("📁 Upload a file")
            uploaded = st.file_uploader(
                "Choose a file (TXT, DOCX, or PDF)",
                type=["txt", "docx", "pdf"],
                help="Files are processed locally and never stored."
            )
            # Show file info if updated
            if uploaded:
                st.info(f"File loaded: **{uploaded.name}** ({uploaded.size / 1024:.1f} KB)")
        
        # Text paste input
        with col2:
            st.subheader("✏ Paste text")
            pasted = st.text_area(
                "Paste your document text here:",
                height=220,
                placeholder="Paste any document text here…",
                help="Paste plain text, Markdown, or any document copy."
            )

        st.markdown("---")

        # Run analysis button
        if st.button("🔍 Run Accessibility Check", type="primary"):
            # Determine text source
            text = None
            source_label = ""

            if uploaded:
                # Extract text from uploaded file
                with st.spinner("Reading file…"):
                    text = extract_text(uploaded)
                    source_label = uploaded.name
            elif pasted and pasted.strip():
                # Use pasted text
                text = pasted.strip()
                source_label = "pasted text"
            else:
                # No input provided
                st.warning("Please upload a file or paste some text first.")

            # Validate and analyze text
            if text and text.strip():
                if len(text.strip()) < 20:
                    # Prevent analyzing very short text
                    st.warning("Text is too short to analyze (need at least 20 characters).")
                else:
                    # Run main checker
                    with st.spinner("Running accessibility checks…"):
                        issues = run_checks(text)

                    # Store in session state
                    st.session_state["analysis_text"]   = text
                    st.session_state["analysis_issues"]  = issues
                    st.session_state["analysis_source"]  = source_label
                    # Clear previous fix state
                    for key in [k for k in st.session_state if k.startswith(("fix_", "acc_", "dis_"))]:
                        del st.session_state[key]

                    # Show success message
                    st.success(f"✅ Analysis complete: **{len(issues)} issue(s)** found in {source_label}")

                    # Show quick summary stats
                    errors   = sum(1 for i in issues if i.severity == "error")
                    warnings = sum(1 for i in issues if i.severity == "warning")
                    infos    = sum(1 for i in issues if i.severity == "info")
                    pc1, pc2, pc3 = st.columns(3)
                    pc1.metric("🔴 Errors",     errors)
                    pc2.metric("🟡 Warnings",   warnings)
                    pc3.metric("🔵 Suggestions", infos)

                    
        st.markdown("---")
        # Results section
        st.title("Analysis Results")

        # If no analysis has been run yet
        if "analysis_issues" not in st.session_state:
            st.info("No analysis has been run yet. Go to **Upload & Analyze** first.")
        else:
            # Load stored results
            issues = st.session_state["analysis_issues"]
            text   = st.session_state["analysis_text"]
            source = st.session_state.get("analysis_source", "document")

            # Show document info
            st.markdown(f"**Source:** {source} &nbsp;|&nbsp; **{len(text.split())} words** &nbsp;|&nbsp; **{len(issues)} issue(s) found**")
            st.markdown("---")

            # Render full interactive report
            render_report(issues, text, api_key)


# Guides and About Pages
elif main_page == "Guides & About":

    # Create tabs for guide sections
    tab1, tab2, tab3 = st.tabs([
        "⚡ Quick Guide",
        "📘 Full Guide",
        "ℹ️ About"
    ])

    # Quick guide tab with simple instructions
    with tab1:
        st.title("Quick User Guide")
        st.markdown("""
        **1. Upload & Analyze**
        Go to *Upload & Analyze* in the sidebar. Upload a TXT/DOCX/PDF file or paste text directly,
        then click **Run Accessibility Check**.

        **2. View Results**
        Navigate to *Analysis Results*. You'll see a summary dashboard showing Errors, Warnings, and Suggestions.

        **3. Browse issues by tab**
        Issues are grouped into three tabs: **Grammar**, **Readability**, and **Accessibility**.
        Use the *All Issues* tab to see everything at once.

        **4. Filter & search**
        Use the filter panel to narrow by category or severity, or search for specific keywords.

        **5. Expand an issue**
        Click any issue row to expand it. You'll see:
        - A plain-English explanation of the problem
        - The problematic text snippet
        - A quick suggested replacement (where applicable)

        **6. Get an AI Fix**
        Add your Anthropic API key in the sidebar, then click **🤖 Get AI Fix** on any issue.
        Review the suggestion and click **✅ Accept this fix** to log it.

        **7. Export**
        Scroll to the bottom of the report to download the original text or a **Fixes Report**
        summarizing every fix you accepted.
        """)

    # Full guide tab with detailed documentation
    with tab2:
        st.title("Full User Guide")
        st.markdown("""
        ### Supported Input

        | Format | Support |
        |--------|---------|
        | Plain text (.txt) | ✅ Full support |
        | Word document (.docx) | ✅ Requires `python-docx` |
        | PDF (.pdf) | ✅ Requires `pdfplumber` |
        | Pasted text | ✅ Always available |

        Maximum recommended file size: **20 MB**.

        ---

        ### What Each Check Does

        **Grammar**
        - *Spelling* — Flags words that may be misspelled and suggests corrections. Proper nouns and
        capitalised words are skipped to reduce false positives.
        - *Repeated words* — Detects unintentional double words (e.g. "the the").
        - *Extra spaces* — Flags multiple consecutive spaces.

        **Readability**
        - *Sentence length* — Flags sentences over 25 words (warning) or 35 words (error).
        - *Flesch Reading Ease* — A 0–100 score: 60+ is suitable for general audiences.
        - *Flesch-Kincaid Grade Level* — U.S. school grade equivalent; aim for Grade 8 or below.
        - *Passive voice* — Flags documents with more than 4 passive constructions.

        **Accessibility**
        - *Inclusive language* — Flags 20+ patterns of non-inclusive phrasing and suggests alternatives.
        - *ALL CAPS overuse* — Flags documents with more than 3 all-caps words (excluding defined acronyms).
        - *Undefined acronyms* — Flags acronyms that never appear in parenthetical definitions.
        - *Missing headings* — Warns when documents over 200 words have no heading structure.
        - *Bare URLs* — Flags URLs used as raw link text (inaccessible to screen readers).

        ---

        ### AI Fix Feature

        Each issue has a **🤖 Get AI Fix** button. This calls **Claude Haiku** to generate a specific,
        context-aware correction. You can:
        - **Accept** the fix → it's logged in the Fixes Report
        - **Dismiss** the issue → it's hidden from the report

        Your API key is used only for the current session and is never stored.

        ---

        ### Exporting

        - **Download original text** — Your source document as plain text.
        - **Download fixes report** — A structured summary of every fix you accepted, showing the original
        snippet alongside the corrected version.

        ---

        ### Accessibility of WriteAble Itself

        - High-contrast badges and colour-coded severity
        - Keyboard-navigable interface via Streamlit
        - Plain-language explanations for every issue
        - No animations or auto-playing media
        """)

    # About tab with project information
    with tab3:
        st.title("About WriteAble")
        st.markdown("""
        WriteAble helps writers, content creators, and teams produce documents that are clearer,
        more inclusive, and accessible to all readers — including people who use assistive technology.

        **Our principles:**
        - Accessibility checks should be *understandable*, not just flagged
        - Plain-language explanations help writers learn, not just   fix
        - AI suggestions should assist human judgment, not replace it

        **Technology stack:**
        - [Streamlit](https://streamlit.io) — UI framework
        - [textstat](https://github.com/textstat/textstat) — Readability metrics
        - [pyspellchecker](https://github.com/barrust/pyspellchecker) — Spelling
        - [Anthropic Claude](https://www.anthropic.com) — AI fix suggestions

        **Standards alignment:**
        - Reading level targets follow [Plain Language Guidelines](https://www.plainlanguage.gov/)
        """)