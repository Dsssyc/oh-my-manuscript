"""LaTeX math to Word Office MathML (OMML) compiler.

Pure-Python, cross-platform (Windows / macOS / Linux) conversion pipeline built
on the pinned ``latex2mathml==3.81.1`` frontend and ``lxml``.  The conversion is
deterministic: identical LaTeX input always yields byte-identical OMML output,
and no external binary (pandoc, XSLT processors, ...) is ever invoked, nor is
any part of Microsoft's closed-source ``MML2OMML.XSL`` embedded.

Pipeline (ADR-001 section 2.5):

1.  **LaTeX pre-scan** -- reject manual numbering (``\\tag``), per-line numbered
    environments (``align`` / ``eqnarray`` / ``gather``) and unknown
    environments, and check brace / ``\\left...\\right`` / environment balance
    *before* anything is converted.  ``aligned`` environments are recognized at
    this top structural layer and assembled into native OMML equation arrays
    (``<m:eqArr>``), because ``latex2mathml==3.81.1`` does not understand
    ``aligned`` and would emit raw ``&`` characters, i.e. invalid XML.
2.  **MathML rendering** -- every single-line fragment is converted to MathML
    by the pinned ``latex2mathml``; residual unresolved backslash commands are
    rejected at this boundary.
3.  **OMML walker** -- the MathML tree is mapped onto ECMA-376 OMML with strict
    child-element ordering, ``Cambria Math`` control properties and explicit
    upright styles for digits, operators and multi-letter function names.

Anything unsupported fails loudly with a stable ``ERR_MATH_*`` error code and
the optional ``block_id``; symbols are never silently dropped, swallowed or
degraded to plain text.
"""

from __future__ import annotations

import re
import unicodedata

from latex2mathml.converter import convert as _latex2mathml_convert
from lxml import etree

__all__ = [
    "M_NS",
    "W_NS",
    "OmmlConversionError",
    "accept_all_revisions",
    "build_numbered_equation_paragraph",
    "build_tracked_omml_replacement",
    "latex_to_omml",
    "reject_all_revisions",
]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = f"{{{W_NS}}}"
_M = f"{{{M_NS}}}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Stable error codes (public surface, asserted by tests).
ERR_TAG_FORBIDDEN = "ERR_MATH_TAG_FORBIDDEN"
ERR_UNSUPPORTED_ENV = "ERR_MATH_UNSUPPORTED_ENV"
ERR_UNKNOWN_COMMAND = "ERR_MATH_UNKNOWN_COMMAND"
ERR_INVALID_LATEX = "ERR_MATH_INVALID_LATEX"
ERR_UNKNOWN_ELEMENT = "ERR_MATH_UNKNOWN_ELEMENT"
ERR_UNSUPPORTED_CONSTRUCT = "ERR_MATH_UNSUPPORTED_CONSTRUCT"

# Right-aligned tab stop (twips) used for the block-level equation number.
_TAB_POS_TWIPS = "9026"

# Environments that carry per-line numbering semantics (ADR-001 2.5: a single
# formula block may carry at most one external block-level number).
_NUMBERED_ENVS = frozenset(
    {"align", "align*", "eqnarray", "eqnarray*", "gather", "gather*"}
)
# Environments natively supported by this compiler.
_ALLOWED_ENVS = frozenset(
    {"matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "cases", "aligned"}
)

_TAG_RE = re.compile(r"\\tag(?![a-zA-Z])")
_BEGIN_RE = re.compile(r"\\begin\s*\{\s*([A-Za-z]+\*?)\s*\}")
_END_RE = re.compile(r"\\end\s*\{\s*([A-Za-z]+\*?)\s*\}")
_WORD_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_ALIGNED_FULL_RE = re.compile(
    r"\\begin\s*\{\s*aligned\s*\}(.*)\\end\s*\{\s*aligned\s*\}", re.DOTALL
)

# N-ary operators mapped onto <m:nary>.  Integrals put their limits beside the
# operator (limLoc=subSup), the others above/below (limLoc=undOvr).
_NARY_CHARS = frozenset({"∑", "∏", "∫", "∬", "∭", "∮", "⋃", "⋂"})
_INTEGRAL_CHARS = frozenset({"∫", "∬", "∭", "∮"})

# Operators that terminate the operand absorbed into a following <m:nary><m:e>
# (mirrors Word's UnicodeMath operand precedence: multiplicative operators and
# parenthesised groups stay inside the operand, additive/relational stop it).
_STOP_OPS = frozenset(
    {
        "+", "-", "−", "±", "∓", "=", "<", ">", "≤", "≥", "≠", "≈", "≡", "≅",
        "∝", "→", "←", "↔", "⇒", "⇐", "⇔", "≪", "≫", "≲", "≳", "⊂", "⊆",
        "⊃", "⊇", ",", ";",
    }
)

# Spacing characters normalized onto dedicated Unicode space glyphs.
_ACCENT_NORMALIZE = {
    "\u005E": "\u02C6",  # ^ CIRCUMFLEX ACCENT -> ˆ MODIFIER LETTER CIRCUMFLEX
    "\u007E": "\u02DC",  # ~ TILDE -> ˜ SMALL TILDE
    "\u2192": "\u20D7",  # → RIGHTWARDS ARROW -> ⃗ COMBINING RIGHT ARROW ABOVE
}
_OVERLINE_CHARS = frozenset({"\u2015", "\u203E"})

# Letterlike symbols that are their own upright glyph (double-struck capitals
# emitted by \mathbb, script capitals emitted by \mathcal, ...).
_UPRIGHT_LETTERLIKE = frozenset(
    "\u2102\u210A\u210B\u210C\u210D"  # ℂ ℊ ℋ ℌ ℍ
    "\u2111\u2112\u2113\u2115\u2119\u211A\u211B\u211C\u211D"
    "\u2124\u212C\u212F\u2130\u2131\u2133\u2134"
)

# MathML mathvariant values -> (m:scr, m:sty) run attributes.
_VARIANT_MAP = {
    "normal": (None, "p"),
    "bold": (None, "b"),
    "italic": (None, None),
    "bold-italic": (None, "bi"),
    "script": ("script", None),
    "bold-script": ("script", "b"),
    "fraktur": ("fraktur", None),
    "bold-fraktur": ("fraktur", "b"),
    "double-struck": ("double-struck", None),
    "sans-serif": ("sans-serif", None),
    "sans-serif-bold": ("sans-serif", "b"),
    "sans-serif-italic": ("sans-serif", "i"),
    "sans-serif-bold-italic": ("sans-serif", "bi"),
    "monospace": ("monospace", None),
}

_DIGIT_WORDS = {
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
    "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9",
}

# MathML mspace widths (named or "<float>em") -> Unicode space glyphs.
_NAMED_WIDTHS = {
    "veryverythinmathspace": "\u200A",
    "verythinmathspace": "\u200A",
    "thinmathspace": "\u2009",
    "mediummathspace": "\u205F",
    "thickmathspace": "\u2005",
    "verythickmathspace": "\u2002",
    "veryverythickmathspace": "\u2002",
    "negativethinmathspace": "\u200B",
    "negativeverythinmathspace": "\u200B",
    "negativeveryverythinmathspace": "\u200B",
    "negativemediummathspace": "\u200B",
    "negativethickmathspace": "\u200B",
}

_SCRIPT_TAGS = frozenset({"msub", "msup", "msubsup", "munder", "mover", "munderover"})

# Delimiter characters recognized around a flattened matrix cluster.
_DELIMITER_CHARS = frozenset("()[]{}|‖⟨⟩")

# Delimiter pairing tracked while absorbing an n-ary operand: nesting pairs and
# symmetric toggles (| and ‖) keep inner additive operators out of stop range.
_OPEN_DELIMS = frozenset("([{⟨")
_CLOSE_DELIMS = frozenset(")]}⟩")
_SYMMETRIC_DELIMS = frozenset({"|", "‖"})

_MATHML_NS = "{http://www.w3.org/1998/Math/MathML}"

# Zero-length MathML linethickness values ("0", "0em", "0.0pt", ...) mark a
# fraction without a bar (\binom, \choose).
_ZERO_LENGTH_RE = re.compile(r"0+(?:\.0*)?(?:px|pt|pc|em|ex|mm|cm|in|%)?")


class OmmlConversionError(Exception):
    """Boundary failure with a stable English error code and optional block_id."""

    def __init__(self, code: str, message: str, block_id: str | None = None) -> None:
        text = f"{code}: {message}"
        if block_id is not None:
            text = f"{text} [block_id={block_id}]"
        super().__init__(text)
        self.code = code
        self.message = message
        self.block_id = block_id


def _err(code: str, message: str, block_id: str | None) -> OmmlConversionError:
    return OmmlConversionError(code, message, block_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def latex_to_omml(
    latex: str, *, display: bool = False, block_id: str | None = None
) -> etree._Element:
    """Compile a LaTeX math fragment to OMML.

    ``display=False`` returns a standalone ``<m:oMath>`` element;
    ``display=True`` returns ``<m:oMathPara><m:oMath>...</m:oMath></m:oMathPara>``.
    Raises :class:`OmmlConversionError` at the first unsupported construct.
    """
    nodes = _latex_to_nodes(latex, block_id, allow_aligned=True)
    if display:
        para = _make_root("oMathPara", _M)
        omath = _sub(para, "oMath")
        omath.extend(nodes)
        return para
    omath = _make_root("oMath", _M)
    omath.extend(nodes)
    return omath


def build_numbered_equation_paragraph(
    latex: str,
    number_text: str | None = None,
    *,
    block_id: str | None = None,
    style_id: str = "DisplayedEquation",
) -> etree._Element:
    """Build a full ``<w:p>`` hosting the display equation and its number.

    With ``number_text`` (e.g. ``"(1)"``) the paragraph carries the equation as
    an inline ``<m:oMath>`` followed by a right tab stop and the number run --
    Word's native pattern for block-level numbered equations.  Without a number
    the paragraph embeds a display ``<m:oMathPara>`` instead.
    """
    paragraph = _make_root("p", _W)
    ppr = _sub(paragraph, "pPr", _W)
    pstyle = _sub(ppr, "pStyle", _W)
    pstyle.set(_W + "val", style_id)
    if number_text:
        tabs = _sub(ppr, "tabs", _W)
        tab = _sub(tabs, "tab", _W)
        tab.set(_W + "val", "right")
        tab.set(_W + "pos", _TAB_POS_TWIPS)
        paragraph.append(latex_to_omml(latex, block_id=block_id))
        tab_run = _sub(paragraph, "r", _W)
        _sub(tab_run, "tab", _W)
        number_run = _sub(paragraph, "r", _W)
        text = _sub(number_run, "t", _W)
        text.set(_XML_SPACE, "preserve")
        text.text = number_text
    else:
        paragraph.append(latex_to_omml(latex, display=True, block_id=block_id))
    return paragraph


def build_tracked_omml_replacement(
    old_latex: str,
    new_latex: str,
    *,
    rev_id_start: int,
    author: str,
    date_utc: str,
    display: bool = False,
    block_id: str | None = None,
) -> tuple[etree._Element, etree._Element, int]:
    """Build an atomic tracked replacement of one formula (ADR-001 2.5).

    Returns ``(w:del, w:ins, next_rev_id)`` where ``w:del`` wraps the old
    ``<m:oMath>`` and ``w:ins`` the new one as siblings-level run content --
    the single ``<m:oMath>`` is the minimal revision atom, never split into
    character-level revisions.  ``next_rev_id`` is the next free revision id.
    """
    deleted = _make_root("del", _W)
    inserted = _make_root("ins", _W)
    for element, rev_id in ((deleted, rev_id_start), (inserted, rev_id_start + 1)):
        element.set(_W + "id", str(rev_id))
        element.set(_W + "author", author)
        element.set(_W + "date", date_utc)
    deleted.append(latex_to_omml(old_latex, display=display, block_id=block_id))
    inserted.append(latex_to_omml(new_latex, display=display, block_id=block_id))
    return deleted, inserted, rev_id_start + 2


def accept_all_revisions(root: etree._Element) -> None:
    """Simulate Word's "Accept All Changes" in place.

    Strips ``<w:ins>`` wrappers (keeping their children) and removes whole
    ``<w:del>`` subtrees.  Scope: the run-level tracked replacements emitted by
    :func:`build_tracked_omml_replacement`; empty property-form ``w:ins`` /
    ``w:del`` markers (paragraph marks) are left untouched.
    """
    for marker in list(root.iter(_W + "ins")):
        if len(marker) == 0:
            continue
        parent = marker.getparent()
        if parent is None:
            continue
        index = parent.index(marker)
        for child in list(marker):
            parent.insert(index, child)
            index += 1
        parent.remove(marker)
    for marker in list(root.iter(_W + "del")):
        if len(marker) == 0:
            continue
        parent = marker.getparent()
        if parent is not None:
            parent.remove(marker)


def reject_all_revisions(root: etree._Element) -> None:
    """Simulate Word's "Reject All Changes" in place (inverse of accept)."""
    for marker in list(root.iter(_W + "ins")):
        if len(marker) == 0:
            continue
        parent = marker.getparent()
        if parent is not None:
            parent.remove(marker)
    for marker in list(root.iter(_W + "del")):
        if len(marker) == 0:
            continue
        parent = marker.getparent()
        if parent is None:
            continue
        index = parent.index(marker)
        for child in list(marker):
            parent.insert(index, child)
            index += 1
        parent.remove(marker)


# ---------------------------------------------------------------------------
# Element construction helpers
# ---------------------------------------------------------------------------


def _make_root(tag: str, ns: str) -> etree._Element:
    """Create a serialization root carrying both namespace declarations."""
    nsmap = {"w": W_NS, "m": M_NS} if ns == _W else {"m": M_NS, "w": W_NS}
    return etree.Element(ns + tag, nsmap=nsmap)


def _frag(tag: str) -> etree._Element:
    """Create a detached OMML fragment (declarations come from the root)."""
    return etree.Element(_M + tag)


def _sub(parent: etree._Element, tag: str, ns: str = _M) -> etree._Element:
    return etree.SubElement(parent, ns + tag)


def _ctrl_pr(parent: etree._Element) -> None:
    """Attach <m:ctrlPr><w:rPr><w:rFonts .../></w:rPr></m:ctrlPr>."""
    ctrl = _sub(parent, "ctrlPr")
    rpr = _sub(ctrl, "rPr", _W)
    fonts = _sub(rpr, "rFonts", _W)
    fonts.set(_W + "ascii", "Cambria Math")
    fonts.set(_W + "hAnsi", "Cambria Math")
    fonts.set(_W + "cs", "Cambria Math")


def _run(
    text: str,
    *,
    scr: str | None = None,
    sty: str | None = None,
    nor: bool = False,
    aln: bool = False,
) -> etree._Element:
    """Create a math run <m:r>[<m:rPr>...</m:rPr>]<m:t>text</m:t></m:r>."""
    run = _frag("r")
    if scr is not None or sty is not None or nor or aln:
        rpr = _sub(run, "rPr")
        if scr is not None:
            _sub(rpr, "scr").set(_M + "val", scr)
        if sty is not None:
            _sub(rpr, "sty").set(_M + "val", sty)
        if nor:
            _sub(rpr, "nor")
        if aln:
            _sub(rpr, "aln")
    t = _sub(run, "t")
    t.set(_XML_SPACE, "preserve")
    t.text = text
    return run


# ---------------------------------------------------------------------------
# LaTeX pre-scan (before latex2mathml sees anything)
# ---------------------------------------------------------------------------


def _strip_math_delimiters(source: str) -> str:
    for begin, end in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if (
            len(source) > len(begin) + len(end)
            and source.startswith(begin)
            and source.endswith(end)
        ):
            return source[len(begin) : -len(end)]
    return source


def _check_balance(source: str, block_id: str | None) -> None:
    """Reject unbalanced braces, \\left...\\right pairs and environments."""
    brace = 0
    left_right = 0
    env_stack: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\\":
            m = _BEGIN_RE.match(source, i)
            if m:
                env_stack.append(m.group(1))
                i = m.end()
                continue
            m = _END_RE.match(source, i)
            if m:
                if not env_stack or env_stack[-1] != m.group(1):
                    raise _err(
                        ERR_INVALID_LATEX,
                        f"unmatched \\end{{{m.group(1)}}}",
                        block_id,
                    )
                env_stack.pop()
                i = m.end()
                continue
            m = _WORD_CMD_RE.match(source, i)
            if m:
                if m.group(1) == "left":
                    left_right += 1
                elif m.group(1) == "right":
                    left_right -= 1
                    if left_right < 0:
                        raise _err(
                            ERR_INVALID_LATEX, "\\right without matching \\left", block_id
                        )
                i = m.end()
                continue
            i += 2  # escaped single character: \{ \} \& \\ ...
            continue
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace < 0:
                raise _err(ERR_INVALID_LATEX, "unbalanced '}'", block_id)
        i += 1
    if brace != 0:
        raise _err(ERR_INVALID_LATEX, "unbalanced braces", block_id)
    if left_right != 0:
        raise _err(ERR_INVALID_LATEX, "unmatched \\left", block_id)
    if env_stack:
        raise _err(
            ERR_INVALID_LATEX, f"unclosed environment '{env_stack[-1]}'", block_id
        )


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split ``text`` at ``sep`` (``"&"`` or ``"\\\\"``) occurring only at the
    top structural level -- outside ``{...}`` groups, ``\\left...\\right``
    pairs and nested ``\\begin{...}`` environments."""
    parts: list[str] = []
    start = 0
    brace = left_right = env = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            if sep == "\\\\" and text.startswith("\\\\", i):
                j = i + 2
                if j < n and text[j] == "*":
                    j += 1
                if j < n and text[j] == "[":
                    close = text.find("]", j)
                    j = n if close == -1 else close + 1
                if brace == left_right == env == 0:
                    parts.append(text[start:i])
                    start = j
                i = j
                continue
            m = _BEGIN_RE.match(text, i)
            if m:
                env += 1
                i = m.end()
                continue
            m = _END_RE.match(text, i)
            if m:
                env = max(env - 1, 0)
                i = m.end()
                continue
            m = _WORD_CMD_RE.match(text, i)
            if m:
                if m.group(1) == "left":
                    left_right += 1
                elif m.group(1) == "right":
                    left_right = max(left_right - 1, 0)
                i = m.end()
                continue
            i += 2
            continue
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(brace - 1, 0)
        elif ch == "&" and sep == "&" and brace == left_right == env == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _check_environments(source: str, block_id: str | None) -> list[str]:
    names = [m.group(1) for m in _BEGIN_RE.finditer(source)]
    for name in names:
        if name in _NUMBERED_ENVS:
            raise _err(
                ERR_UNSUPPORTED_ENV,
                f"environment '{name}' numbers every line; split into multiple "
                "formula blocks or use 'aligned'",
                block_id,
            )
        if name not in _ALLOWED_ENVS:
            raise _err(
                ERR_UNSUPPORTED_ENV, f"unsupported LaTeX environment '{name}'", block_id
            )
    return names


def _latex_to_nodes(
    latex: str, block_id: str | None, *, allow_aligned: bool
) -> list[etree._Element]:
    source = _strip_math_delimiters(latex.strip()).strip()
    if not source:
        return []
    if _TAG_RE.search(source):
        raise _err(
            ERR_TAG_FORBIDDEN,
            r"manual numbering via \tag is forbidden; use block-level numbering",
            block_id,
        )
    _check_balance(source, block_id)
    env_names = _check_environments(source, block_id)
    if "aligned" in env_names:
        if not allow_aligned:
            raise _err(
                ERR_UNSUPPORTED_ENV, "nested 'aligned' is not supported", block_id
            )
        return [_build_aligned(source, block_id)]
    math = _latex_to_mathml_tree(source, block_id)
    return _convert_children(list(math), block_id)


def _build_aligned(source: str, block_id: str | None) -> etree._Element:
    """Assemble a whole-formula ``aligned`` environment into <m:eqArr>."""
    match = _ALIGNED_FULL_RE.fullmatch(source)
    if match is None:
        raise _err(
            ERR_UNSUPPORTED_ENV,
            "'aligned' must span the entire formula block",
            block_id,
        )
    body = match.group(1)
    if any(m.group(1) == "aligned" for m in _BEGIN_RE.finditer(body)):
        raise _err(
            ERR_UNSUPPORTED_ENV, "nested 'aligned' is not supported", block_id
        )
    eq_arr = _frag("eqArr")
    eq_arr_pr = _sub(eq_arr, "eqArrPr")
    _ctrl_pr(eq_arr_pr)
    for row in _split_top_level(body, "\\\\"):
        row_element = _sub(eq_arr, "e")
        for cell_index, cell in enumerate(_split_top_level(row, "&")):
            if cell_index:
                row_element.append(_run("", aln=True))
            row_element.extend(
                _latex_to_nodes(cell, block_id, allow_aligned=False)
            )
    return eq_arr


def _latex_to_mathml_tree(
    source: str, block_id: str | None
) -> etree._Element:
    """Convert one single-line fragment to a parsed MathML tree."""
    try:
        xml_text = _latex2mathml_convert(source)
    except OmmlConversionError:
        raise
    except Exception as exc:  # latex2mathml raises assorted private errors
        raise _err(
            ERR_INVALID_LATEX,
            f"latex2mathml failed: {type(exc).__name__}: {exc}",
            block_id,
        ) from exc
    try:
        root = etree.fromstring(xml_text)
    except etree.XMLSyntaxError as exc:
        raise _err(
            ERR_INVALID_LATEX,
            f"latex2mathml emitted malformed XML: {exc}",
            block_id,
        ) from exc
    for text in root.itertext():
        if "\\" in text:
            snippet = text.strip()[:40]
            raise _err(
                ERR_UNKNOWN_COMMAND,
                f"unresolved LaTeX command in converter output: '{snippet}'",
                block_id,
            )
    return root


def _mathml_to_nodes(xml_text: str, block_id: str | None) -> list[etree._Element]:
    """Parse MathML text and convert it (validation boundary; used by tests)."""
    root = etree.fromstring(xml_text)
    for text in root.itertext():
        if "\\" in text:
            raise _err(
                ERR_UNKNOWN_COMMAND,
                f"unresolved LaTeX command: '{text.strip()[:40]}'",
                block_id,
            )
    return _convert_children(list(root), block_id)


# ---------------------------------------------------------------------------
# MathML -> OMML walker
# ---------------------------------------------------------------------------


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _core(element: etree._Element) -> etree._Element:
    """Unwrap a single-child <mrow> (latex2mathml wraps bases variably)."""
    if _local(element) == "mrow" and len(element) == 1:
        return element[0]
    return element


def _is_nary_base(core: etree._Element) -> bool:
    return _local(core) == "mo" and (core.text or "") in _NARY_CHARS


def _is_name_blob(element: etree._Element) -> bool:
    """An \\operatorname-style blob: letters, digits, operator text and spaces."""
    allowed = {"mi", "mo", "mn", "mspace"}
    kids = list(element)
    if not kids or any(_local(k) not in allowed for k in kids):
        return False
    return all((k.text or "") != "" or _local(k) == "mspace" for k in kids)


def _is_limit_base(core: etree._Element, marker: bool) -> bool:
    tag = _local(core)
    text = core.text or ""
    if tag == "mo" and len(text) >= 2:
        return True
    if marker and tag == "mi" and len(text) >= 2:
        return True
    return marker and tag == "mrow" and _is_name_blob(core)


def _name_runs(blob: etree._Element, block_id: str | None) -> list[etree._Element]:
    parts: list[str] = []
    for child in blob:
        if _local(child) == "mspace":
            parts.append(_space_char(child.get("width", ""), block_id))
        else:
            parts.append(child.text or "")
    return [_run("".join(parts), sty="p")]


def _space_char(width: str, block_id: str | None) -> str:
    named = _NAMED_WIDTHS.get(width)
    if named is not None:
        return named
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)em", width)
    if m is None:
        raise _err(
            ERR_UNSUPPORTED_CONSTRUCT,
            f"unsupported MathML space width '{width}'",
            block_id,
        )
    value = float(m.group(1))
    if value <= 0:
        return "\u200B"
    if value <= 0.18:
        return "\u2009"  # thin space
    if value <= 0.24:
        return "\u205F"  # medium mathematical space
    if value <= 0.30:
        return "\u2005"  # four-per-em space
    if value <= 0.45:
        return "\u2004"  # three-per-em space
    if value <= 0.70:
        return "\u2002"  # en space
    return "\u2003"  # em space


def _decode_math_alphanumeric(ch: str) -> tuple[str, str | None, str | None] | None:
    """Decode plane-1 Mathematical Alphanumeric chars to (base, scr, sty)."""
    codepoint = ord(ch)
    if not 0x1D400 <= codepoint <= 0x1D7FF:
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    if not name.startswith("MATHEMATICAL "):
        return None
    words = name.split()[1:]
    base: str | None = None
    head = words
    if len(words) >= 2 and words[-2] in ("CAPITAL", "SMALL"):
        tail = words[-1]
        if len(tail) == 1 and tail.isalpha():
            # Unicode names always spell the letter in capitals ("...SMALL Q");
            # lowercase letters must be restored explicitly.
            base = tail.lower() if words[-2] == "SMALL" else tail
        else:
            case = "SMALL" if words[-2] == "SMALL" else "CAPITAL"
            try:
                base = unicodedata.lookup(f"GREEK {case} LETTER {tail}")
            except KeyError:
                return None
        head = words[:-2]
    elif len(words) >= 2 and words[-2] == "DIGIT":
        base = _DIGIT_WORDS.get(words[-1])
        head = words[:-2]
    if base is None:
        return None
    style = "-".join(head).lower()
    scr, sty = _VARIANT_MAP.get(style, (None, None))
    return base, scr, sty


def _is_no_bar_fraction(node: etree._Element) -> bool:
    """A <mfrac> with zero linethickness renders without a fraction bar."""
    value = node.get("linethickness")
    if value is None or not value.strip():
        return False
    return _ZERO_LENGTH_RE.fullmatch(value.strip()) is not None


def _group_matrix_clusters(
    children: list[etree._Element],
) -> list[etree._Element]:
    """Regroup flattened delimiter-wrapped constructs into synthetic mrows.

    Inside a larger expression ``latex2mathml`` flattens ``\\begin{pmatrix}``
    (and friends) into bare sibling ``<mo>(</mo> <mtable/> <mo>)</mo>``
    sequences, and ``\\binom{n}{k}`` into ``<mo>(</mo> <mfrac linethickness="0"/>
    <mo>)</mo>``; wrapping them back keeps the delimiter pairing intact both for
    direct row conversion and inside n-ary operand absorption.
    """
    grouped: list[etree._Element] = []
    i = 0
    count = len(children)

    def _triple(middle_is_no_bar_frac: bool) -> bool:
        if i + 2 >= count:
            return False
        middle = children[i + 1]
        middle_ok = (
            _is_no_bar_fraction(middle)
            if middle_is_no_bar_frac
            else _local(middle) == "mtable"
        )
        return (
            _local(children[i + 2]) == "mo"
            and (children[i + 2].text or "") in _DELIMITER_CHARS
            and middle_ok
        )

    while i < count:
        node = children[i]
        if (
            _local(node) == "mo"
            and (node.text or "") in _DELIMITER_CHARS
            and (
                _triple(middle_is_no_bar_frac=False)
                or _triple(middle_is_no_bar_frac=True)
            )
        ):
            wrapper = etree.Element(_MATHML_NS + "mrow")
            wrapper.extend([node, children[i + 1], children[i + 2]])
            grouped.append(wrapper)
            i += 3
            continue
        if (
            _local(node) == "mo"
            and node.get("fence") == "true"
            and node.get("form") == "prefix"
            and i + 1 < count
            and _local(children[i + 1]) == "mtable"
        ):
            wrapper = etree.Element(_MATHML_NS + "mrow")
            wrapper.extend([node, children[i + 1]])
            grouped.append(wrapper)
            i += 2
            continue
        grouped.append(node)
        i += 1
    return grouped


def _convert_children(
    children: list[etree._Element], block_id: str | None
) -> list[etree._Element]:
    grouped = _group_matrix_clusters(children)
    out: list[etree._Element] = []
    marker = False  # a preceding empty <mo/> flags an \operatorname construct
    i = 0
    count = len(grouped)
    while i < count:
        node = grouped[i]
        tag = _local(node)
        if tag == "mo" and not (node.text or ""):
            marker = True
            i += 1
            continue
        if tag in _SCRIPT_TAGS:
            core = _core(node[0])
            if _is_nary_base(core):
                operand, end = _absorb_operand(grouped, i + 1, block_id)
                _, sub, sup = _script_parts(node)
                out.append(_build_nary_op(core.text or "", sub, sup, operand, block_id))
                i = end
                marker = False
                continue
            if _is_limit_base(core, marker):
                out.append(_build_limit(node, core, marker, block_id))
                marker = False
                i += 1
                continue
            if tag in ("mover", "munder"):
                out.append(_build_over_under(node, block_id))
                marker = False
                i += 1
                continue
            if tag == "munderover":
                raise _err(
                    ERR_UNSUPPORTED_CONSTRUCT,
                    "munder/munderover without n-ary or limit base is not supported",
                    block_id,
                )
            out.append(_build_plain_script(node, block_id))
            marker = False
            i += 1
            continue
        if tag == "mo" and (node.text or "") in _NARY_CHARS:
            # A bare n-ary operator without limits still maps onto <m:nary>.
            operand, end = _absorb_operand(grouped, i + 1, block_id)
            out.append(
                _build_nary_op(node.text or "", None, None, operand, block_id)
            )
            i = end
            marker = False
            continue
        if marker and tag == "mrow" and _is_name_blob(node):
            out.extend(_name_runs(node, block_id))
            marker = False
            i += 1
            continue
        marker = False
        out.extend(_convert_node(node, block_id))
        i += 1
    return out


def _convert_node(
    node: etree._Element, block_id: str | None
) -> list[etree._Element]:
    tag = _local(node)
    if tag in _SCRIPT_TAGS:
        core = _core(node[0])
        if _is_nary_base(core):
            _, sub, sup = _script_parts(node)
            return [_build_nary_op(core.text or "", sub, sup, [], block_id)]
        if _is_limit_base(core, False):
            return [_build_limit(node, core, False, block_id)]
        if tag in ("mover", "munder"):
            return [_build_over_under(node, block_id)]
        return [_build_plain_script(node, block_id)]
    if tag == "mrow":
        kids = list(node)
        delimited = _match_delimited(kids, block_id)
        if delimited is not None:
            return delimited
        if kids and all(
            _local(k) == "mi" and k.get("mathvariant") == "normal" and (k.text or "")
            for k in kids
        ):
            # \mathrm{...}: consecutive upright letters merge into one run.
            return [_run("".join(k.text or "" for k in kids), sty="p")]
        return _convert_children(kids, block_id)
    if tag == "mstyle":
        return _convert_children(list(node), block_id)
    if tag == "mfrac":
        return [_build_fraction(node, block_id)]
    if tag in ("msqrt", "mroot"):
        return [_build_radical(node, block_id)]
    if tag == "mtable":
        return [_build_matrix(node, block_id)]
    if tag == "mo":
        text = node.text or ""
        if text in _NARY_CHARS:
            return [_build_nary_op(text, None, None, [], block_id)]
        return [] if not text else [_run(text, sty="p")]
    if tag == "mi":
        return _mi_runs(node, block_id)
    if tag == "mn":
        text = node.text or ""
        return [] if not text else [_run(text, sty="p")]
    if tag == "mtext":
        text = node.text or ""
        return [] if not text else [_run(text, sty="p", nor=True)]
    if tag == "mspace":
        if node.get("linebreak") == "newline":
            raise _err(
                ERR_UNSUPPORTED_CONSTRUCT,
                "line breaks require an 'aligned' environment",
                block_id,
            )
        return [_run(_space_char(node.get("width", ""), block_id))]
    raise _err(ERR_UNKNOWN_ELEMENT, f"unsupported MathML element '{tag}'", block_id)


def _mi_runs(node: etree._Element, block_id: str | None) -> list[etree._Element]:
    text = node.text or ""
    if not text:
        return []
    variant = node.get("mathvariant")
    if len(text) == 1:
        if variant is not None:
            if variant not in _VARIANT_MAP:
                raise _err(
                    ERR_UNSUPPORTED_CONSTRUCT,
                    f"unsupported mathvariant '{variant}'",
                    block_id,
                )
            scr, sty = _VARIANT_MAP[variant]
            return [_run(text, scr=scr, sty=sty)]
        decoded = _decode_math_alphanumeric(text)
        if decoded is not None:
            base, scr, sty = decoded
            return [_run(base, scr=scr, sty=sty)]
        if text in _UPRIGHT_LETTERLIKE:
            return [_run(text, sty="p")]
        return [_run(text)]
    return [_run(text, sty="p")]


def _delimiter_char(child: etree._Element) -> str:
    """The single delimiter character a sibling contributes, if any.

    Besides bare ``<mo>``/``<mi>`` delimiters this covers delimiters carrying
    scripts, e.g. ``)^2`` arriving as ``<msup><mo>)</mo><mn>2</mn></msup>`` and
    ``\\|_2^2`` as ``<msubsup><mo>‖</mo>...`` -- latex2mathml attaches the
    scripts to the delimiter itself.
    """
    tag = _local(child)
    if tag in ("mo", "mi"):
        text = child.text or ""
        return text if len(text) == 1 else ""
    if tag in _SCRIPT_TAGS:
        core = _core(child[0])
        if _local(core) in ("mo", "mi"):
            text = core.text or ""
            return text if len(text) == 1 else ""
    return ""


def _absorb_operand(
    children: list[etree._Element], start: int, block_id: str | None
) -> tuple[list[etree._Element], int]:
    """Collect the operand of an n-ary operator from its following siblings.

    Absorption stops at a top-level additive/relational operator (Word's
    UnicodeMath operand rule) and swallows parenthesised groups whole.  Depth
    tracking covers nesting pairs (``([{`` / ``)]}``, ``⟨`` / ``⟩``) including
    scripted closing delimiters, and the symmetric ``|`` / ``‖`` delimiters of
    norms and absolute values toggle in and out of the operand scope.
    """
    nodes: list[etree._Element] = []
    depth = 0
    symmetric = False
    i = start
    while i < len(children):
        child = children[i]
        if (
            depth == 0
            and not symmetric
            and _local(child) == "mo"
            and (child.text or "") in _STOP_OPS
        ):
            break
        char = _delimiter_char(child)
        if char in _OPEN_DELIMS:
            depth += 1
        elif char in _CLOSE_DELIMS:
            depth = max(depth - 1, 0)
        elif char in _SYMMETRIC_DELIMS:
            symmetric = not symmetric
        nodes.extend(_convert_node(child, block_id))
        i += 1
    return nodes, i


def _script_parts(
    node: etree._Element,
) -> tuple[etree._Element, etree._Element | None, etree._Element | None]:
    """Return (base, sub, sup) elements of a MathML script construct."""
    tag = _local(node)
    sub = node[1] if tag in ("msub", "msubsup", "munder", "munderover") else None
    sup = None
    if tag in ("msubsup", "munderover"):
        sup = node[2]
    elif tag in ("msup", "mover"):
        sup = node[1]
    return node[0], sub, sup


def _build_nary_op(
    op_char: str,
    sub: etree._Element | None,
    sup: etree._Element | None,
    operand: list[etree._Element],
    block_id: str | None,
) -> etree._Element:
    nary = _frag("nary")
    nary_pr = _sub(nary, "naryPr")
    _sub(nary_pr, "chr").set(_M + "val", op_char)
    _sub(nary_pr, "limLoc").set(
        _M + "val", "subSup" if op_char in _INTEGRAL_CHARS else "undOvr"
    )
    if sub is None:
        _sub(nary_pr, "subHide").set(_M + "val", "1")
    if sup is None:
        _sub(nary_pr, "supHide").set(_M + "val", "1")
    _ctrl_pr(nary_pr)
    sub_element = _sub(nary, "sub")
    if sub is not None:
        sub_element.extend(_convert_node(sub, block_id))
    sup_element = _sub(nary, "sup")
    if sup is not None:
        sup_element.extend(_convert_node(sup, block_id))
    _sub(nary, "e").extend(operand)
    return nary


def _build_limit(
    node: etree._Element,
    core: etree._Element,
    marker: bool,
    block_id: str | None,
) -> etree._Element:
    tag = _local(node)
    if _local(core) == "mrow":
        base_nodes = _name_runs(core, block_id)
    else:
        base_nodes = [_run(core.text or "", sty="p")]
    _, sub, sup = _script_parts(node)
    if tag in ("msubsup", "munderover"):
        return _assemble_script("sSubSup", base_nodes, sub, sup, block_id)
    upper = tag in ("msup", "mover")
    element = _frag("limUpp" if upper else "limLow")
    _ctrl_pr(_sub(element, "limUppPr" if upper else "limLowPr"))
    _sub(element, "e").extend(base_nodes)
    script = sup if upper else sub
    _sub(element, "lim").extend(_convert_node(script, block_id))
    return element


def _build_over_under(node: etree._Element, block_id: str | None) -> etree._Element:
    """mover/munder: accent, over/under-bar, or generic over/underset."""
    tag = _local(node)
    over = tag == "mover"
    base, script = node[0], node[1]
    core = _core(script)
    base_nodes = _convert_node(base, block_id)
    if _local(core) == "mo" and (core.text or ""):
        char = core.text or ""
        if over and char in _OVERLINE_CHARS:
            return _assemble_bar(base_nodes, "top")
        if not over and (char in _OVERLINE_CHARS or char == "\u0332"):
            return _assemble_bar(base_nodes, "bot")
        if over:
            accent = _frag("acc")
            _sub(_sub(accent, "accPr"), "chr").set(
                _M + "val", _ACCENT_NORMALIZE.get(char, char)
            )
            _assemble_ctrl(accent, "accPr")
            _sub(accent, "e").extend(base_nodes)
            return accent
        raise _err(
            ERR_UNSUPPORTED_CONSTRUCT,
            f"below-accent '{char}' is not supported",
            block_id,
        )
    element = _frag("limUpp" if over else "limLow")
    _ctrl_pr(_sub(element, "limUppPr" if over else "limLowPr"))
    _sub(element, "e").extend(base_nodes)
    _sub(element, "lim").extend(_convert_node(script, block_id))
    return element


def _assemble_ctrl(element: etree._Element, pr_tag: str) -> None:
    # Reuse the pr element already created for chr insertion ordering.
    pr = element.find(_M + pr_tag)
    _ctrl_pr(pr)


def _assemble_bar(base_nodes: list[etree._Element], pos: str) -> etree._Element:
    bar = _frag("bar")
    _sub(_sub(bar, "barPr"), "pos").set(_M + "val", pos)
    _assemble_ctrl(bar, "barPr")
    _sub(bar, "e").extend(base_nodes)
    return bar


def _build_plain_script(node: etree._Element, block_id: str | None) -> etree._Element:
    tag = _local(node)
    kind = {"msub": "sSub", "msup": "sSup", "msubsup": "sSubSup"}[tag]
    base, sub, sup = _script_parts(node)
    base_nodes = _convert_node(base, block_id)
    return _assemble_script(kind, base_nodes, sub, sup, block_id)


def _assemble_script(
    kind: str,
    base_nodes: list[etree._Element],
    sub: etree._Element | None,
    sup: etree._Element | None,
    block_id: str | None,
) -> etree._Element:
    element = _frag(kind)
    _ctrl_pr(_sub(element, kind + "Pr"))
    _sub(element, "e").extend(base_nodes)
    if kind in ("sSub", "sSubSup"):
        _sub(element, "sub").extend(_convert_node(sub, block_id))
    if kind in ("sSup", "sSubSup"):
        _sub(element, "sup").extend(_convert_node(sup, block_id))
    return element


def _build_fraction(node: etree._Element, block_id: str | None) -> etree._Element:
    kids = list(node)
    if len(kids) != 2:
        raise _err(
            ERR_INVALID_LATEX, "fraction requires a numerator and a denominator", block_id
        )
    frac = _frag("f")
    f_pr = _sub(frac, "fPr")
    if _is_no_bar_fraction(node):
        # \binom / \choose: ECMA-376 order is m:type before m:ctrlPr.
        _sub(f_pr, "type").set(_M + "val", "noBar")
    _ctrl_pr(f_pr)
    _sub(frac, "num").extend(_convert_node(kids[0], block_id))
    _sub(frac, "den").extend(_convert_node(kids[1], block_id))
    return frac


def _build_radical(node: etree._Element, block_id: str | None) -> etree._Element:
    rad = _frag("rad")
    rad_pr = _sub(rad, "radPr")
    if _local(node) == "msqrt":
        _sub(rad_pr, "degHide").set(_M + "val", "1")
        _ctrl_pr(rad_pr)
        _sub(rad, "deg")  # ECMA-376 keeps an explicit empty <m:deg/>
        _sub(rad, "e").extend(_convert_node(node[0], block_id))
        return rad
    kids = list(node)
    if len(kids) != 2:
        raise _err(
            ERR_INVALID_LATEX, "root requires a radicand and a degree", block_id
        )
    _ctrl_pr(rad_pr)
    _sub(rad, "deg").extend(_convert_node(kids[1], block_id))
    _sub(rad, "e").extend(_convert_node(kids[0], block_id))
    return rad


def _match_delimited(
    kids: list[etree._Element], block_id: str | None
) -> list[etree._Element] | None:
    """Detect \\left...\\right groups, delimiter-wrapped matrices and bare
    matrices (including the one-sided cases brace) at <mrow> level."""

    def _fence(el: etree._Element, form: str) -> bool:
        return (
            _local(el) == "mo"
            and el.get("fence") == "true"
            and el.get("form") == form
        )

    if len(kids) >= 2 and _fence(kids[0], "prefix"):
        begin, end = kids[0].text or "", ""
        if _fence(kids[-1], "postfix"):
            end = kids[-1].text or ""
            return [_assemble_delimiter(begin, end, kids[1:-1], block_id)]
        if len(kids) == 2 and _local(kids[1]) == "mtable":
            return [_assemble_delimiter(begin, "", [kids[1]], block_id)]
        return None
    if (
        len(kids) == 3
        and _local(kids[0]) == "mo"
        and _local(kids[1]) == "mtable"
        and _local(kids[2]) == "mo"
    ):
        return [
            _assemble_delimiter(
                kids[0].text or "", kids[2].text or "", [kids[1]], block_id
            )
        ]
    if (
        len(kids) == 3
        and _local(kids[0]) == "mo"
        and _local(kids[1]) == "mfrac"
        and _local(kids[2]) == "mo"
        and _is_no_bar_fraction(kids[1])
    ):
        # \binom{n}{k}: enlarged parentheses around a bar-less fraction.
        return [
            _assemble_delimiter(
                kids[0].text or "", kids[2].text or "", [kids[1]], block_id
            )
        ]
    if len(kids) == 1 and _local(kids[0]) == "mtable":
        return [_build_matrix(kids[0], block_id)]
    return None


def _assemble_delimiter(
    begin: str, end: str, content: list[etree._Element], block_id: str | None
) -> etree._Element:
    delimiter = _frag("d")
    d_pr = _sub(delimiter, "dPr")
    if begin != "(":
        _sub(d_pr, "begChr").set(_M + "val", begin)
    if end != ")":
        _sub(d_pr, "endChr").set(_M + "val", end)
    _ctrl_pr(d_pr)
    _sub(delimiter, "e").extend(_convert_children(content, block_id))
    return delimiter


def _build_matrix(mtable: etree._Element, block_id: str | None) -> etree._Element:
    rows = [child for child in mtable if _local(child) == "mtr"]
    if not rows:
        raise _err(ERR_INVALID_LATEX, "matrix without rows", block_id)
    cells = [[c for c in row if _local(c) == "mtd"] for row in rows]
    columns = max(len(row_cells) for row_cells in cells)
    if columns == 0:
        raise _err(ERR_INVALID_LATEX, "matrix without columns", block_id)
    all_left = all(
        cell.get("columnalign") == "left" for row_cells in cells for cell in row_cells
    )
    matrix = _frag("m")
    m_pr = _sub(matrix, "mPr")
    mc_pr = _sub(_sub(_sub(m_pr, "mcs"), "mc"), "mcPr")
    _sub(mc_pr, "count").set(_M + "val", str(columns))
    _sub(mc_pr, "mcJc").set(_M + "val", "left" if all_left else "center")
    for row_cells in cells:
        mr = _sub(matrix, "mr")
        for cell in row_cells:
            _sub(mr, "e").extend(_convert_children(list(cell), block_id))
    return matrix
