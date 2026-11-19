"""Strict OOXML/ECMA-376 tests for the LaTeX -> OMML compiler (`omm.omml`)."""

from __future__ import annotations

import re
import zipfile
from copy import deepcopy

import pytest
from lxml import etree

from omm import omml as omml_module
from omm.omml import (
    M_NS,
    W_NS,
    OmmlConversionError,
    accept_all_revisions,
    build_numbered_equation_paragraph,
    build_tracked_omml_replacement,
    latex_to_omml,
    reject_all_revisions,
)

M = f"{{{M_NS}}}"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
NS = {"m": M_NS, "w": W_NS}
VAL = M + "val"


# ---------------------------------------------------------------------------
# Strict ECMA-376 child-order / attribute / nesting validator
# ---------------------------------------------------------------------------

_MATH_CONTENT = (
    "m:r", "m:f", "m:rad", "m:nary", "m:sSub", "m:sSup", "m:sSubSup",
    "m:d", "m:m", "m:acc", "m:bar", "m:limLow", "m:limUpp", "m:eqArr",
)

_SCHEMA: dict[str, str] = {
    "m:oMath": "*",
    "m:oMathPara": "oMathParaPr? oMath+",
    "m:oMathParaPr": "jc?",
    "m:r": "rPr? t+",
    "m:rPr": "nor? scr? sty? aln?",
    "m:f": "fPr? num den",
    "m:fPr": "type? ctrlPr?",
    "m:type": "",
    "m:rad": "radPr? deg? e",
    "m:radPr": "degHide? ctrlPr?",
    "m:nary": "naryPr? sub? sup? e",
    "m:naryPr": "chr? limLoc? grow? subHide? supHide? ctrlPr?",
    "m:sSub": "sSubPr? e sub",
    "m:sSup": "sSupPr? e sup",
    "m:sSubSup": "sSubSupPr? e sub sup",
    "m:sSubPr": "ctrlPr?",
    "m:sSupPr": "ctrlPr?",
    "m:sSubSupPr": "ctrlPr?",
    "m:d": "dPr? e+",
    "m:dPr": "begChr? sepChr? endChr? grow? shp? ctrlPr?",
    "m:m": "mPr? mr+",
    "m:mPr": "mcs? mpr? ctrlPr?",
    "m:mcs": "mc+",
    "m:mc": "mcPr",
    "m:mcPr": "count? mcJc?",
    "m:mr": "e+",
    "m:acc": "accPr? e",
    "m:accPr": "chr? ctrlPr?",
    "m:bar": "barPr? e",
    "m:barPr": "pos? ctrlPr?",
    "m:limLow": "limLowPr? e lim",
    "m:limLowPr": "ctrlPr?",
    "m:limUpp": "limUppPr? e lim",
    "m:limUppPr": "ctrlPr?",
    "m:eqArr": "eqArrPr? e+",
    "m:eqArrPr": "baseJc? maxDist? objDist? rSpRule? rSp? ctrlPr?",
    "m:ctrlPr": "(w:rPr)?",
    "m:lim": "*",
    "m:num": "*",
    "m:den": "*",
    "m:deg": "*",
    "m:sub": "*",
    "m:sup": "*",
    "m:e": "*",
    "m:t": "",
    "m:nor": "",
    "m:aln": "",
    "m:chr": "",
    "m:begChr": "",
    "m:sepChr": "",
    "m:endChr": "",
    "m:limLoc": "",
    "m:degHide": "",
    "m:subHide": "",
    "m:supHide": "",
    "m:count": "",
    "m:mcJc": "",
    "m:pos": "",
    "m:sty": "",
    "m:scr": "",
    "m:jc": "",
    "w:p": "w:pPr? (m:oMath|m:oMathPara|w:r|w:del|w:ins)*",
    "w:pPr": "w:pStyle? w:tabs?",
    "w:pStyle": "",
    "w:tabs": "w:tab+",
    "w:tab": "",
    "w:r": "rPr? (w:tab|w:t)*",
    "w:rPr": "w:rFonts?",
    "w:rFonts": "",
    "w:t": "",
    "w:del": "(m:oMath|m:oMathPara)+",
    "w:ins": "(m:oMath|m:oMathPara)+",
}

_ATTRS: dict[str, set[str]] = {
    "m:t": {XML_SPACE},
    "w:t": {XML_SPACE},
    "m:chr": {VAL},
    "m:begChr": {VAL},
    "m:sepChr": {VAL},
    "m:endChr": {VAL},
    "m:limLoc": {VAL},
    "m:degHide": {VAL},
    "m:subHide": {VAL},
    "m:supHide": {VAL},
    "m:count": {VAL},
    "m:mcJc": {VAL},
    "m:pos": {VAL},
    "m:sty": {VAL},
    "m:scr": {VAL},
    "m:type": {VAL},
    "w:pStyle": {W + "val"},
    "w:tab": {W + "val", W + "pos", W + "leader"},
    "w:del": {W + "id", W + "author", W + "date"},
    "w:ins": {W + "id", W + "author", W + "date"},
    "w:rFonts": {W + "ascii", W + "hAnsi", W + "cs"},
}


def _qname(element: etree._Element) -> str:
    q = etree.QName(element)
    prefix = "m" if q.namespace == M_NS else "w"
    return f"{prefix}:{q.localname}"


_Token = tuple[tuple[str, ...], str]


def _tokenize_pattern(pattern: str) -> list[_Token]:
    tokens: list[_Token] = []

    def qualified(name: str) -> str:
        return name if ":" in name else f"m:{name}"

    for chunk in pattern.split():
        if chunk == "*":
            tokens.append((_MATH_CONTENT, "*"))
            continue
        grouped = re.fullmatch(r"\(([^)]+)\)([*+?]?)", chunk)
        if grouped is not None:
            names = tuple(qualified(x) for x in grouped.group(1).split("|"))
            tokens.append((names, grouped.group(2)))
            continue
        simple = re.fullmatch(r"([^*?+]+)([*+?]?)", chunk)
        assert simple is not None, chunk
        tokens.append(((qualified(simple.group(1)),), simple.group(2)))
    return tokens


def _sequence_matches(tokens: list[_Token], children: list[str]) -> bool:
    if not tokens:
        return not children
    alternatives, quantifier = tokens[0]
    rest = tokens[1:]
    if quantifier in ("", "?"):
        if children and children[0] in alternatives:
            return _sequence_matches(rest, children[1:])
        return quantifier == "?" and _sequence_matches(rest, children)
    taken = 0
    while taken < len(children) and children[taken] in alternatives:
        taken += 1
    if quantifier == "+" and taken == 0:
        return False
    minimum = 0 if quantifier == "*" else 1
    for count in range(taken, minimum - 1, -1):
        if _sequence_matches(rest, children[count:]):
            return True
    return False


_PATTERNS = {tag: _tokenize_pattern(pat) for tag, pat in _SCHEMA.items()}


def assert_ecma376(root: etree._Element) -> None:
    """Assert strict child order, attributes and text placement."""
    for element in root.iter():
        tag = _qname(element)
        assert tag in _PATTERNS, f"element {tag} outside the covered ECMA-376 schema"
        children = [_qname(child) for child in element]
        assert _sequence_matches(_PATTERNS[tag], children), (
            f"{tag} violates ECMA-376 child order: [{children}]"
        )
        if tag == "m:rPr" and "m:nor" in children:
            assert {"m:scr", "m:sty"}.isdisjoint(children), (
                "m:nor is mutually exclusive with math script and style"
            )
        for attr in element.attrib:
            assert attr in _ATTRS.get(tag, set()), f"{tag} has stray attribute {attr}"
        if tag in ("m:t", "w:t"):
            assert element.get(XML_SPACE) == "preserve", f"{tag} must preserve space"
        else:
            assert not (element.text or "").strip(), f"{tag} carries stray text"
        assert not (element.tail or "").strip(), f"{tag} carries a stray tail"


def _texts(element: etree._Element) -> str:
    return "".join(element.itertext())


def _find(root: etree._Element, path: str) -> etree._Element | None:
    return root.find(path, namespaces=NS)


def _findall(root: etree._Element, path: str) -> list[etree._Element]:
    return root.findall(path, namespaces=NS)


def _canonical(element: etree._Element) -> bytes:
    """Canonical bytes: re-parse standalone first (c14n2 needs declared scope)."""
    standalone = etree.fromstring(etree.tostring(element))
    return etree.tostring(standalone, method="c14n2")


def _tags(elements: list[etree._Element]) -> list[str]:
    return [_qname(e) for e in elements]


# ---------------------------------------------------------------------------
# Validator self-checks
# ---------------------------------------------------------------------------


def test_validator_rejects_wrong_child_order() -> None:
    bad = etree.Element(M + "f", nsmap={"m": M_NS})
    etree.SubElement(bad, M + "num")
    etree.SubElement(bad, M + "den")
    etree.SubElement(bad, M + "fPr")  # must precede num/den
    with pytest.raises(AssertionError, match="child order"):
        assert_ecma376(bad)


def test_validator_rejects_missing_required_child() -> None:
    bad = etree.Element(M + "nary", nsmap={"m": M_NS})
    etree.SubElement(bad, M + "naryPr")
    with pytest.raises(AssertionError):
        assert_ecma376(bad)


def test_validator_rejects_stray_text_and_attributes() -> None:
    bad = etree.Element(M + "oMath", nsmap={"m": M_NS})
    bad.text = "stray"
    with pytest.raises(AssertionError, match="stray text"):
        assert_ecma376(bad)

    bad2 = etree.Element(M + "oMath", nsmap={"m": M_NS})
    run = etree.SubElement(bad2, M + "r")
    etree.SubElement(run, M + "t").text = "x"
    run.set(M + "bogus", "1")
    with pytest.raises(AssertionError, match="stray attribute"):
        assert_ecma376(bad2)


# ---------------------------------------------------------------------------
# Runs, styles and fonts
# ---------------------------------------------------------------------------


def test_single_latin_letter_is_default_math_italic() -> None:
    root = latex_to_omml("x")
    assert_ecma376(root)
    run = _find(root, "m:r")
    assert run is not None and run.find("m:rPr", NS) is None
    assert _texts(run) == "x"


def test_digits_and_operators_are_explicitly_upright() -> None:
    root = latex_to_omml(r"0.90 + a \le 1")
    assert_ecma376(root)
    runs = {("".join(r.itertext())): r for r in _findall(root, "m:r")}
    for text in ("0.90", "+", "\u2264", "1"):
        assert runs[text].find("m:rPr/m:sty", NS).get(VAL) == "p"
    assert runs["a"].find("m:rPr", NS) is None


def test_multiletter_function_names_are_upright() -> None:
    root = latex_to_omml(r"\sin x + \ln y")
    assert_ecma376(root)
    sin_run = _find(root, "m:r")
    assert _texts(sin_run) == "sin"
    assert _find(sin_run, "m:rPr/m:sty").get(VAL) == "p"


def test_mathrm_merges_into_single_upright_run() -> None:
    root = latex_to_omml(r"\mathrm{NSE}")
    assert_ecma376(root)
    assert len(_findall(root, "m:r")) == 1
    run = _find(root, "m:r")
    assert _texts(run) == "NSE"
    assert _find(run, "m:rPr/m:sty").get(VAL) == "p"


def test_mathbf_maps_to_bold_style() -> None:
    root = latex_to_omml(r"\mathbf{K}")
    run = _find(root, "m:r")
    assert _texts(run) == "K"
    assert _find(run, "m:rPr/m:sty").get(VAL) == "b"


def test_mathbf_preserves_lowercase_letters() -> None:
    # U+1D42A is "MATHEMATICAL BOLD SMALL Q"; the letter in the Unicode name
    # is always spelled in capitals and must be restored to lowercase.
    root = latex_to_omml(r"\mathbf{q}")
    assert_ecma376(root)
    run = _find(root, "m:r")
    assert _texts(run) == "q"
    assert _find(run, "m:rPr/m:sty").get(VAL) == "b"


def test_mathbf_lowercase_vector_differential() -> None:
    root = latex_to_omml(r"d\mathbf{s}")
    assert_ecma376(root)
    runs = _findall(root, "m:r")
    assert _texts(runs[0]) == "d"
    assert runs[0].find("m:rPr", NS) is None
    assert _texts(runs[1]) == "s"
    assert _find(runs[1], "m:rPr/m:sty").get(VAL) == "b"


def test_mathsf_and_mathcal_preserve_lowercase() -> None:
    root = latex_to_omml(r"\mathsf{x} + \mathcal{l}")
    assert_ecma376(root)
    runs = [r for r in _findall(root, "m:r") if r.find("m:rPr", NS) is not None]
    sans = next(r for r in runs if _find(r, "m:rPr/m:scr") is not None)
    assert _texts(sans) == "x"
    assert _find(sans, "m:rPr/m:scr").get(VAL) == "sans-serif"
    script = [
        r
        for r in runs
        if _find(r, "m:rPr/m:scr") is not None
        and _find(r, "m:rPr/m:scr").get(VAL) == "script"
    ]
    assert [_texts(r) for r in script] == ["l"]


def test_mathbf_lowercase_with_subscript() -> None:
    root = latex_to_omml(r"\mathbf{x}_i")
    assert_ecma376(root)
    base = _find(root, "m:sSub/m:e/m:r")
    assert _texts(base) == "x"
    assert _find(base, "m:rPr/m:sty").get(VAL) == "b"


def test_boldsymbol_greek_maps_to_bold_italic() -> None:
    root = latex_to_omml(r"\boldsymbol{\alpha}")
    run = _find(root, "m:r")
    assert _texts(run) == "\u03B1"
    assert _find(run, "m:rPr/m:sty").get(VAL) == "bi"


def test_mathbb_and_mathcal_keep_upright_letterlike_glyphs() -> None:
    root = latex_to_omml(r"\mathbb{R}, \mathcal{L}")
    runs = _findall(root, "m:r")
    assert _texts(runs[0]) == "\u211D"
    assert _texts(runs[2]) == "\u2112"
    for run in (runs[0], runs[2]):
        assert _find(run, "m:rPr/m:sty").get(VAL) == "p"


def test_text_run_is_normal_text_with_preserved_space() -> None:
    root = latex_to_omml(r"\text{otherwise } x")
    assert_ecma376(root)
    runs = _findall(root, "m:r")
    assert _texts(runs[0]) == "otherwise\u00A0"
    rpr = runs[0].find("m:rPr", NS)
    assert _tags(list(rpr)) == ["m:nor"]
    assert runs[0].find("m:t", NS).get(XML_SPACE) == "preserve"
    assert runs[1].find("m:rPr", NS) is None


def test_control_properties_use_cambria_math_fonts() -> None:
    root = latex_to_omml(r"\frac{\sqrt{x}}{\sum_i a}")
    assert_ecma376(root)
    ctrl_prs = _findall(root, ".//m:ctrlPr")
    assert ctrl_prs
    for ctrl in ctrl_prs:
        fonts = ctrl.find("w:rPr/w:rFonts", NS)
        assert fonts is not None
        assert fonts.get(W + "ascii") == "Cambria Math"
        assert fonts.get(W + "hAnsi") == "Cambria Math"
        assert fonts.get(W + "cs") == "Cambria Math"


# ---------------------------------------------------------------------------
# Fractions, radicals, scripts
# ---------------------------------------------------------------------------


def test_fraction_child_order() -> None:
    root = latex_to_omml(r"\frac{a}{b}")
    assert_ecma376(root)
    frac = _find(root, "m:f")
    assert _tags(list(frac)) == ["m:fPr", "m:num", "m:den"]
    assert _texts(_find(frac, "m:num")) == "a"
    assert _texts(_find(frac, "m:den")) == "b"


def test_dfrac_and_tfrac_are_plain_fractions() -> None:
    for latex in (r"\dfrac{\partial u}{\partial t}", r"\tfrac{a}{b}"):
        root = latex_to_omml(latex)
        assert_ecma376(root)
        assert _find(root, "m:f") is not None


def test_binom_maps_to_delimited_no_bar_fraction() -> None:
    # latex2mathml renders \binom{n}{k} as enlarged parentheses around
    # <mfrac linethickness="0">; the zero-width bar must become
    # <m:type m:val="noBar"/> (before m:ctrlPr) and the parentheses an m:d.
    root = latex_to_omml(r"\binom{n}{k}")
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:d"]
    assert _tags(list(_find(root, "m:d/m:dPr"))) == ["m:ctrlPr"]  # default ( )
    frac = _find(root, "m:d/m:e/m:f")
    assert _tags(list(_find(frac, "m:fPr"))) == ["m:type", "m:ctrlPr"]
    assert _find(frac, "m:fPr/m:type").get(VAL) == "noBar"
    assert _texts(_find(frac, "m:num")) == "n"
    assert _texts(_find(frac, "m:den")) == "k"


def test_binom_inside_larger_formula_keeps_delimiters() -> None:
    root = latex_to_omml(r"p(X) = \binom{n}{k} p^{k} (1 - p)^{n - k}")
    assert_ecma376(root)
    delimiters = _findall(root, ".//m:d")
    assert len(delimiters) == 1  # only the binom wraps; flat parens stay runs
    assert _texts(_find(delimiters[0], "m:e/m:f")) == "nk"


def test_square_root_hides_degree_but_keeps_empty_deg() -> None:
    root = latex_to_omml(r"\sqrt{x}")
    assert_ecma376(root)
    rad = _find(root, "m:rad")
    assert _tags(list(rad)) == ["m:radPr", "m:deg", "m:e"]
    assert _find(rad, "m:radPr/m:degHide").get(VAL) == "1"
    deg = _find(rad, "m:deg")
    assert len(deg) == 0  # ECMA-376 keeps the empty <m:deg/> placeholder
    assert _texts(_find(rad, "m:e")) == "x"


def test_nth_root_exposes_degree() -> None:
    root = latex_to_omml(r"\sqrt[n]{x}")
    rad = _find(root, "m:rad")
    assert _find(rad, "m:radPr/m:degHide") is None
    assert _texts(_find(rad, "m:deg")) == "n"
    assert _texts(_find(rad, "m:e")) == "x"


def test_script_child_orders() -> None:
    root = latex_to_omml("x_i")
    assert _tags(list(_find(root, "m:sSub"))) == ["m:sSubPr", "m:e", "m:sub"]
    root = latex_to_omml("x^2")
    assert _tags(list(_find(root, "m:sSup"))) == ["m:sSupPr", "m:e", "m:sup"]
    root = latex_to_omml("x_i^2")
    sub_sup = _find(root, "m:sSubSup")
    assert _tags(list(sub_sup)) == ["m:sSubSupPr", "m:e", "m:sub", "m:sup"]
    assert _texts(_find(sub_sup, "m:sub")) == "i"
    assert _texts(_find(sub_sup, "m:sup")) == "2"


# ---------------------------------------------------------------------------
# N-ary operators and limits
# ---------------------------------------------------------------------------


def test_integral_with_limits_and_operand() -> None:
    root = latex_to_omml(r"\int_0^\infty e^{-x}\,dx = \sqrt{\pi}")
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:nary", "m:r", "m:rad"]  # '=' stops absorption
    nary = _find(root, "m:nary")
    nary_pr = _find(nary, "m:naryPr")
    assert _tags(list(nary_pr)) == [
        "m:chr", "m:limLoc", "m:ctrlPr",
    ]
    assert _find(nary_pr, "m:chr").get(VAL) == "\u222B"
    assert _find(nary_pr, "m:limLoc").get(VAL) == "subSup"
    assert _texts(_find(nary, "m:sub")) == "0"
    assert _texts(_find(nary, "m:sup")) == "\u221E"
    operand = _find(nary, "m:e")
    assert _find(operand, "m:sSup") is not None
    assert "\u2009" in _texts(operand)  # thin space from \,
    assert _texts(operand).endswith("dx")


def test_sum_without_limits_hides_but_keeps_sub_sup() -> None:
    root = latex_to_omml(r"\sum x_i")
    assert_ecma376(root)
    nary = _find(root, "m:nary")
    nary_pr = _find(nary, "m:naryPr")
    assert _tags(list(nary_pr)) == [
        "m:chr", "m:limLoc", "m:subHide", "m:supHide", "m:ctrlPr",
    ]
    assert _find(nary_pr, "m:chr").get(VAL) == "\u2211"
    assert _find(nary_pr, "m:limLoc").get(VAL) == "undOvr"
    assert _find(nary_pr, "m:subHide").get(VAL) == "1"
    assert _find(nary_pr, "m:supHide").get(VAL) == "1"
    assert len(_find(nary, "m:sub")) == 0  # empty <m:sub/> still emitted
    assert len(_find(nary, "m:sup")) == 0
    assert _texts(_find(nary, "m:e")) == "xi"


def test_sum_with_limits() -> None:
    root = latex_to_omml(r"\sum_{i=1}^{n} x_i")
    nary = _find(root, "m:nary")
    assert _find(nary, "m:naryPr/m:subHide") is None
    assert _find(nary, "m:naryPr/m:supHide") is None
    assert _texts(_find(nary, "m:sub")) == "i=1"
    assert _texts(_find(nary, "m:sup")) == "n"


def test_sum_operand_stops_at_equals_after_scripted_close_paren() -> None:
    # latex2mathml emits ")^2" as <msup><mo>)</mo><mn>2</mn></msup>; the
    # scripted closing delimiter must still return absorption to depth 0 so
    # "= \sigma^2" stays outside <m:nary>.
    root = latex_to_omml(r"\sum_{i=1}^{n} (x_i - \mu)^2 = \sigma^2")
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:nary", "m:r", "m:sSup"]
    assert _texts(_find(root, "m:nary/m:e")) == "(xi\u2212\u03BC)2"
    assert _texts(root).endswith("=\u03C32")


def test_sum_operand_stops_at_equals_after_scripted_norm_delimiter() -> None:
    # The closing norm delimiter arrives as <msubsup><mo>\u2016</mo><mn>2</mn>
    # <mn>2</mn></msubsup>; the inner minus must not stop absorption early.
    root = latex_to_omml(r"\sum_{i=1}^{n} \| \mathbf{x}_i - \mathbf{y}_i \|_2^2 = 0")
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:nary", "m:r", "m:r"]
    assert _texts(_find(root, "m:nary/m:e")) == (
        "\u2016xi\u2212yi\u201622"  # ‖x_i − y_i‖₂² with bold x/y, scripts 2,2
    )
    assert _texts(root).endswith("=0")


def test_sum_operand_stops_at_plus_after_bare_absolute_value() -> None:
    root = latex_to_omml(r"\sum_i |a - b| + c")
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:nary", "m:r", "m:r"]
    assert _texts(_find(root, "m:nary/m:e")) == "|a\u2212b|"
    assert _texts(root).endswith("+c")


def test_nash_sutcliffe_efficiency_keeps_operands_inside_their_sums() -> None:
    root = latex_to_omml(NSE, block_id="eq-nse")
    assert_ecma376(root)
    fraction = _find(root, "m:f")
    numerator = _find(fraction, "m:num/m:nary")
    denominator = _find(fraction, "m:den/m:nary")
    assert numerator is not None and denominator is not None
    assert _texts(_find(numerator, "m:e")) == "(oi\u2212si)2"
    assert _texts(_find(denominator, "m:e")) == "(oi\u2212o)2"  # \bar{o} accent kept
    assert _find(denominator, "m:e/m:acc") is not None


def test_lim_maps_to_lim_low_without_swallowing_operand() -> None:
    root = latex_to_omml(r"\lim_{x \to 0} f(x)")
    assert_ecma376(root)
    assert _tags(list(root))[0] == "m:limLow"
    lim_low = _find(root, "m:limLow")
    assert _tags(list(lim_low)) == ["m:limLowPr", "m:e", "m:lim"]
    assert _texts(_find(lim_low, "m:e")) == "lim"
    assert _find(lim_low, "m:e/m:r/m:rPr/m:sty").get(VAL) == "p"
    assert _texts(_find(lim_low, "m:lim")) == "x\u21920"
    # f(x) stays a sibling outside <m:limLow>, matching Word semantics
    assert _texts(root)[len("limx\u21920"):] == "f(x)"


def test_min_with_subscript_is_lim_low() -> None:
    root = latex_to_omml(r"\min_e \Delta t_e")
    lim_low = _find(root, "m:limLow")
    assert _texts(_find(lim_low, "m:e")) == "min"
    assert _texts(_find(lim_low, "m:lim")) == "e"
    assert_ecma376(root)


def test_operatorname_with_subscript_is_lim_low() -> None:
    root = latex_to_omml(r"\operatorname{arg\,min}_{\theta}")
    assert_ecma376(root)
    lim_low = _find(root, "m:limLow")
    base = _texts(_find(lim_low, "m:e"))
    assert base == "arg\u2009min"
    assert _find(lim_low, "m:e/m:r/m:rPr/m:sty").get(VAL) == "p"
    assert _texts(_find(lim_low, "m:lim")) == "\u03B8"


# ---------------------------------------------------------------------------
# Accents and bars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latex", "char"),
    [
        (r"\hat{Q}", "\u02C6"),
        (r"\tilde{n}", "\u02DC"),
        (r"\vec{v}", "\u20D7"),
        (r"\bar{y}", "\u00AF"),
        (r"\dot{x}", "\u02D9"),
        (r"\ddot{x}", "\u00A8"),
    ],
)
def test_accent_characters_are_normalized(latex: str, char: str) -> None:
    root = latex_to_omml(latex)
    assert_ecma376(root)
    acc = _find(root, "m:acc")
    assert _tags(list(acc)) == ["m:accPr", "m:e"]
    assert _find(acc, "m:accPr/m:chr").get(VAL) == char


def test_overline_is_top_bar() -> None:
    root = latex_to_omml(r"\overline{x}")
    assert_ecma376(root)
    bar = _find(root, "m:bar")
    assert _tags(list(bar)) == ["m:barPr", "m:e"]
    assert _find(bar, "m:barPr/m:pos").get(VAL) == "top"


def test_hat_with_max_subscript_nests_acc_inside_ssub() -> None:
    root = latex_to_omml(r"\hat{Q}_{\max}")
    assert_ecma376(root)
    sub = _find(root, "m:sSub")
    assert _find(sub, "m:e/m:acc") is not None
    assert _texts(_find(sub, "m:sub")) == "max"
    assert _find(sub, "m:sub/m:r/m:rPr/m:sty").get(VAL) == "p"


# ---------------------------------------------------------------------------
# Delimiters and matrices
# ---------------------------------------------------------------------------


def test_paren_delimiter_uses_ecma_defaults() -> None:
    root = latex_to_omml(r"\left( \frac{a}{b} \right)")
    assert_ecma376(root)
    delimiter = _find(root, "m:d")
    assert _tags(list(delimiter)) == ["m:dPr", "m:e"]
    dpr = _find(delimiter, "m:dPr")
    assert _tags(list(dpr)) == ["m:ctrlPr"]  # "(" / ")" are the schema defaults
    assert _find(delimiter, "m:e/m:f") is not None


@pytest.mark.parametrize(
    ("latex", "begin", "end"),
    [
        (r"\left| x \right|", "|", "|"),
        (r"\left\| x \right\|", "\u2016", "\u2016"),
        (r"\left[ x \right]", "[", "]"),
        (r"\left\{ x \right\}", "{", "}"),
        (r"\left\langle x \right\rangle", "\u27E8", "\u27E9"),
    ],
)
def test_explicit_delimiter_characters(latex: str, begin: str, end: str) -> None:
    root = latex_to_omml(latex)
    assert_ecma376(root)
    dpr = _find(root, "m:d/m:dPr")
    assert _tags(list(dpr)) == ["m:begChr", "m:endChr", "m:ctrlPr"]
    assert _find(dpr, "m:begChr").get(VAL) == begin
    assert _find(dpr, "m:endChr").get(VAL) == end


def test_invisible_delimiter_side_is_empty_string() -> None:
    root = latex_to_omml(r"\left. x \right)")
    dpr = _find(root, "m:d/m:dPr")
    assert _find(dpr, "m:begChr").get(VAL) == ""
    assert _find(dpr, "m:endChr") is None


def test_delimited_group_can_carry_scripts() -> None:
    root = latex_to_omml(r"\left( x + y \right)^2")
    assert_ecma376(root)
    sup = _find(root, "m:sSup")
    assert _find(sup, "m:e/m:d") is not None
    assert _texts(_find(sup, "m:sup")) == "2"


@pytest.mark.parametrize(
    ("latex", "begin", "end"),
    [
        (r"\begin{matrix} a & b \\ c & d \end{matrix}", None, None),
        (r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}", None, None),
        (r"\begin{bmatrix} a & b \\ c & d \end{bmatrix}", "[", "]"),
        (r"\begin{Bmatrix} a & b \\ c & d \end{Bmatrix}", "{", "}"),
        (r"\begin{vmatrix} a & b \\ c & d \end{vmatrix}", "|", "|"),
        (r"\begin{Vmatrix} a & b \\ c & d \end{Vmatrix}", "\u2016", "\u2016"),
    ],
)
def test_matrix_environments(latex: str, begin: str | None, end: str | None) -> None:
    root = latex_to_omml(latex)
    assert_ecma376(root)
    if begin is None:
        matrix = _find(root, ".//m:m")
        assert matrix is not None
    else:
        dpr = _find(root, "m:d/m:dPr")
        assert _find(dpr, "m:begChr").get(VAL) == begin
        assert _find(dpr, "m:endChr").get(VAL) == end
        matrix = _find(root, "m:d/m:e/m:m")
    assert _tags(list(matrix))[:1] == ["m:mPr"]
    assert _find(matrix, "m:mPr/m:mcs/m:mc/m:mcPr/m:count").get(VAL) == "2"
    assert _find(matrix, "m:mPr/m:mcs/m:mc/m:mcPr/m:mcJc").get(VAL) == "center"
    rows = _findall(matrix, "m:mr")
    assert len(rows) == 2
    assert [_texts(e) for e in _findall(rows[0], "m:e")] == ["a", "b"]


def test_flattened_pmatrix_after_big_operator_keeps_delimiters() -> None:
    root = latex_to_omml(r"\int_0^1 \begin{pmatrix} a \\ b \end{pmatrix} dx")
    assert_ecma376(root)
    operand = _find(root, "m:nary/m:e")
    assert _find(operand, "m:d") is not None
    assert _find(operand, "m:d/m:e/m:m") is not None
    assert _texts(_find(operand, "m:d/m:e/m:m")) == "ab"


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


CASES_LATEX = (
    r"\begin{cases} q, & \text{if } t \le t_p \\ q_{\max}, & \text{otherwise} \end{cases}"
)


def test_cases_maps_to_one_sided_brace_and_left_aligned_matrix() -> None:
    root = latex_to_omml(CASES_LATEX)
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:d"]
    dpr = _find(root, "m:d/m:dPr")
    assert _tags(list(dpr)) == ["m:begChr", "m:endChr", "m:ctrlPr"]
    assert _find(dpr, "m:begChr").get(VAL) == "{"
    assert _find(dpr, "m:endChr").get(VAL) == ""
    matrix = _find(root, "m:d/m:e/m:m")
    mc_pr = _find(matrix, "m:mPr/m:mcs/m:mc/m:mcPr")
    assert _find(mc_pr, "m:count").get(VAL) == "2"
    assert _find(mc_pr, "m:mcJc").get(VAL) == "left"
    rows = _findall(matrix, "m:mr")
    assert len(rows) == 2
    assert "q," in _texts(rows[0])
    assert "\u03B8" not in _texts(root)
    text_runs = [
        r for r in _findall(root, ".//m:r") if _find(r, "m:rPr/m:nor") is not None
    ]
    assert [_texts(r) for r in text_runs] == ["if\u00A0", "otherwise"]


def test_cases_embedded_in_larger_formula() -> None:
    root = latex_to_omml(r"f(t) = " + CASES_LATEX)
    assert_ecma376(root)
    delimiter = _find(root, "m:d")
    assert _find(delimiter, "m:dPr/m:begChr").get(VAL) == "{"


# ---------------------------------------------------------------------------
# aligned -> m:eqArr
# ---------------------------------------------------------------------------


ALIGNED_LATEX = (
    r"\begin{aligned} "
    r"\frac{\partial q}{\partial t} &= -c\,\frac{\partial q}{\partial x} \\ "
    r"\mathbf{u} &= \begin{pmatrix} u \\ v \end{pmatrix} "
    r"\end{aligned}"
)


def test_aligned_maps_to_eq_arr_with_alignment_markers() -> None:
    root = latex_to_omml(ALIGNED_LATEX)
    assert_ecma376(root)
    assert _tags(list(root)) == ["m:eqArr"]
    eq_arr = _find(root, "m:eqArr")
    assert _tags(list(eq_arr))[0] == "m:eqArrPr"
    rows = _findall(eq_arr, "m:e")
    assert len(rows) == 2  # the inner pmatrix '\\' must NOT create extra rows
    for row in rows:
        markers = [
            r
            for r in _findall(row, "m:r")
            if _find(r, "m:rPr/m:aln") is not None
        ]
        assert len(markers) == 1
        assert _tags(list(markers[0])) == ["m:rPr", "m:t"]
    assert _find(rows[0], "m:f") is not None
    nested = _find(rows[1], "m:d/m:e/m:m")
    assert nested is not None
    assert len(_findall(nested, "m:mr")) == 2


def test_aligned_ampersand_inside_nested_matrix_is_protected() -> None:
    latex = (
        r"\begin{aligned} \mathbf{A} &= \begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix} "
        r"+ \left| x \right| \end{aligned}"
    )
    root = latex_to_omml(latex)
    assert_ecma376(root)
    rows = _findall(root, "m:eqArr/m:e")
    assert len(rows) == 1
    row = rows[0]
    markers = [r for r in _findall(row, "m:r") if _find(r, "m:rPr/m:aln") is not None]
    assert len(markers) == 1  # only the top-level '&' is an alignment point
    matrix = _find(row, "m:d/m:e/m:m")
    assert _find(matrix, "m:mPr/m:mcs/m:mc/m:mcPr/m:count").get(VAL) == "2"
    abs_delimiters = [
        d
        for d in _findall(row, ".//m:d")
        if _find(d, "m:dPr/m:begChr") is not None
        and _find(d, "m:dPr/m:begChr").get(VAL) == "|"
    ]
    assert len(abs_delimiters) == 1  # \left| x \right| kept its own delimiters


def test_aligned_must_span_whole_block() -> None:
    with pytest.raises(OmmlConversionError) as excinfo:
        latex_to_omml(r"x + \begin{aligned} a &= b \end{aligned}", block_id="eq-a")
    assert excinfo.value.code == "ERR_MATH_UNSUPPORTED_ENV"
    assert excinfo.value.block_id == "eq-a"


# ---------------------------------------------------------------------------
# Display mode, numbered paragraphs, tracked revisions
# ---------------------------------------------------------------------------


def test_inline_vs_display_wrapping() -> None:
    inline = latex_to_omml(r"\frac{a}{b}")
    assert _qname(inline) == "m:oMath"
    assert_ecma376(inline)
    display = latex_to_omml(r"\frac{a}{b}", display=True)
    assert _qname(display) == "m:oMathPara"
    assert _tags(list(display)) == ["m:oMath"]
    assert_ecma376(display)


def test_empty_formula_yields_empty_omath() -> None:
    root = latex_to_omml("   ")
    assert _qname(root) == "m:oMath"
    assert len(root) == 0
    assert_ecma376(root)


def test_numbered_equation_paragraph_layout() -> None:
    paragraph = build_numbered_equation_paragraph(
        r"\frac{a}{b}", "(1)", block_id="eq-1"
    )
    assert_ecma376(paragraph)
    assert _tags(list(paragraph)) == ["w:pPr", "m:oMath", "w:r", "w:r"]
    assert _tags(list(_find(paragraph, "w:pPr"))) == ["w:pStyle", "w:tabs"]
    assert _find(paragraph, "w:pPr/w:pStyle").get(W + "val") == "DisplayedEquation"
    tab = _find(paragraph, "w:pPr/w:tabs/w:tab")
    assert tab.get(W + "val") == "right"
    assert tab.get(W + "pos") == "9026"
    runs = _findall(paragraph, "w:r")
    assert _find(runs[0], "w:tab") is not None
    assert _texts(runs[1]) == "(1)"


def test_numbered_equation_paragraph_custom_style_and_no_number() -> None:
    paragraph = build_numbered_equation_paragraph(
        r"\frac{a}{b}", block_id="eq-2", style_id="Equation"
    )
    assert_ecma376(paragraph)
    assert _tags(list(paragraph)) == ["w:pPr", "m:oMathPara"]
    assert _find(paragraph, "w:pPr/w:pStyle").get(W + "val") == "Equation"
    assert _find(paragraph, "w:pPr/w:tabs") is None


AUTHOR = "Reviewer 2"
DATE = "2026-11-18T00:00:00Z"
OLD_LATEX = r"\Delta t \le \frac{\Delta x}{|u| + \sqrt{g h}}"
NEW_LATEX = r"\Delta t \le \min_e \frac{C_r\,\Delta x_e}{|u_e| + \sqrt{g\,h_e}}"


def _wrap(*children: etree._Element) -> etree._Element:
    paragraph = etree.Element(W + "p", nsmap={"w": W_NS, "m": M_NS})
    for child in children:
        paragraph.append(child)
    return paragraph


def test_tracked_replacement_marks_and_ids() -> None:
    deleted, inserted, next_id = build_tracked_omml_replacement(
        OLD_LATEX,
        NEW_LATEX,
        rev_id_start=101,
        author=AUTHOR,
        date_utc=DATE,
        block_id="eq-x",
    )
    for element, rev_id in ((deleted, "101"), (inserted, "102")):
        assert _qname(element) in ("w:del", "w:ins")
        assert element.get(W + "id") == rev_id
        assert element.get(W + "author") == AUTHOR
        assert element.get(W + "date") == DATE
        assert _tags(list(element)) == ["m:oMath"]
    assert next_id == 103
    assert deleted.tail is None and inserted.tail is None
    assert_ecma376(_wrap(deepcopy(deleted), deepcopy(inserted)))


def test_tracked_replacement_display_wraps_omath_para() -> None:
    deleted, _, _ = build_tracked_omml_replacement(
        OLD_LATEX,
        NEW_LATEX,
        rev_id_start=1,
        author=AUTHOR,
        date_utc=DATE,
        display=True,
    )
    assert _tags(list(deleted)) == ["m:oMathPara"]
    assert _find(deleted, "m:oMathPara/m:oMath") is not None


def test_accept_all_revisions_restores_new_formula_bytes() -> None:
    deleted, inserted, _ = build_tracked_omml_replacement(
        OLD_LATEX, NEW_LATEX, rev_id_start=1, author=AUTHOR, date_utc=DATE
    )
    paragraph = _wrap(deleted, inserted)
    accept_all_revisions(paragraph)
    assert _find(paragraph, "w:del") is None
    assert _find(paragraph, "w:ins") is None
    omath = _find(paragraph, "m:oMath")
    assert omath is not None
    assert _canonical(omath) == _canonical(latex_to_omml(NEW_LATEX))


def test_reject_all_revisions_restores_old_formula_bytes() -> None:
    deleted, inserted, _ = build_tracked_omml_replacement(
        OLD_LATEX, NEW_LATEX, rev_id_start=1, author=AUTHOR, date_utc=DATE
    )
    paragraph = _wrap(deleted, inserted)
    reject_all_revisions(paragraph)
    assert _find(paragraph, "w:del") is None
    assert _find(paragraph, "w:ins") is None
    omath = _find(paragraph, "m:oMath")
    assert omath is not None
    assert _canonical(omath) == _canonical(latex_to_omml(OLD_LATEX))


def test_accept_reject_round_trip_display_variant() -> None:
    deleted, inserted, _ = build_tracked_omml_replacement(
        OLD_LATEX,
        NEW_LATEX,
        rev_id_start=7,
        author=AUTHOR,
        date_utc=DATE,
        display=True,
    )
    accepted = _wrap(deepcopy(deleted), deepcopy(inserted))
    accept_all_revisions(accepted)
    para = _find(accepted, "m:oMathPara")
    assert para is not None
    assert _canonical(para) == _canonical(
        latex_to_omml(NEW_LATEX, display=True)
    )
    rejected = _wrap(deleted, inserted)
    reject_all_revisions(rejected)
    assert _canonical(_find(rejected, "m:oMathPara")) == _canonical(
        latex_to_omml(OLD_LATEX, display=True)
    )


def test_conversion_is_byte_deterministic() -> None:
    latex = r"\frac{\partial h}{\partial t} + \sum_{i=1}^{n} \hat{Q}_i"
    assert etree.tostring(latex_to_omml(latex)) == etree.tostring(latex_to_omml(latex))
    assert etree.tostring(latex_to_omml(latex, display=True)) == etree.tostring(
        latex_to_omml(latex, display=True)
    )


# ---------------------------------------------------------------------------
# Negative boundary discipline
# ---------------------------------------------------------------------------


def _expect_error(latex: str, code: str, block_id: str | None = "eq-neg") -> None:
    with pytest.raises(OmmlConversionError) as excinfo:
        latex_to_omml(latex, block_id=block_id)
    assert excinfo.value.code == code
    assert excinfo.value.block_id == block_id
    assert code in str(excinfo.value)
    if block_id is not None:
        assert block_id in str(excinfo.value)


@pytest.mark.parametrize("latex", [r"\tag{1}", r"x = y \tag*{2}"])
def test_tag_is_forbidden(latex: str) -> None:
    _expect_error(latex, "ERR_MATH_TAG_FORBIDDEN")


@pytest.mark.parametrize(
    "latex",
    [
        r"\begin{align} a &= b \end{align}",
        r"\begin{align*} a &= b \end{align*}",
        r"\begin{eqnarray} a &= b \end{eqnarray}",
        r"\begin{gather} a \\ b \end{gather}",
    ],
)
def test_numbered_multiline_environments_rejected(latex: str) -> None:
    _expect_error(latex, "ERR_MATH_UNSUPPORTED_ENV")


@pytest.mark.parametrize(
    "latex",
    [
        r"\begin{array}{cc} a & b \\ c & d \end{array}",
        r"\begin{tikzpicture} a \end{tikzpicture}",
        r"\begin{unknown} a \end{unknown}",
        r"\begin{split} a &= b \end{split}",
    ],
)
def test_unknown_environments_rejected(latex: str) -> None:
    _expect_error(latex, "ERR_MATH_UNSUPPORTED_ENV")


@pytest.mark.parametrize("latex", [r"\tikz{x}", r"\foo{x} + 1", r"\unknownmacro"])
def test_unresolved_commands_rejected(latex: str) -> None:
    _expect_error(latex, "ERR_MATH_UNKNOWN_COMMAND")


@pytest.mark.parametrize(
    "latex",
    [
        r"\frac{a",  # unbalanced brace (latex2mathml would stay silent)
        r"\frac{a}",  # missing denominator
        r"x^{",  # latex2mathml raises its own error
        r"a & b",  # emits invalid XML (<mi>&</mi>)
        r"\left( x",  # unmatched \left
        r"\right)x",  # \right without \left
        r"\begin{cases} a",  # unclosed environment
    ],
)
def test_invalid_latex_rejected(latex: str) -> None:
    _expect_error(latex, "ERR_MATH_INVALID_LATEX")


def test_bare_linebreak_rejected() -> None:
    _expect_error(r"x \\ y", "ERR_MATH_UNSUPPORTED_CONSTRUCT")


def test_nested_aligned_rejected() -> None:
    latex = r"\begin{aligned} a &= \begin{aligned} b &= c \end{aligned} \end{aligned}"
    _expect_error(latex, "ERR_MATH_UNSUPPORTED_ENV")


def test_unknown_mathml_element_rejected() -> None:
    mathml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><ms>stray</ms></math>'
    )
    with pytest.raises(OmmlConversionError) as excinfo:
        omml_module._mathml_to_nodes(mathml, "eq-neg")
    assert excinfo.value.code == "ERR_MATH_UNKNOWN_ELEMENT"
    assert "ms" in excinfo.value.message
    assert excinfo.value.block_id == "eq-neg"


def test_error_without_block_id() -> None:
    with pytest.raises(OmmlConversionError) as excinfo:
        latex_to_omml(r"\tag{9}")
    assert excinfo.value.block_id is None
    assert excinfo.value.code == "ERR_MATH_TAG_FORBIDDEN"


# ---------------------------------------------------------------------------
# Representative hydrology / geoscience / mathematics formulas
# ---------------------------------------------------------------------------


BOUSSINESQ = (
    r"\frac{\partial h}{\partial t}"
    r" = \frac{1}{S_y} \frac{\partial}{\partial x}"
    r"\left( K(h) \frac{\partial h}{\partial x} \right)"
    r" + \frac{R}{S_y}"
)

CFL = (
    r"\Delta t \le \min_e \frac{C_r\,\Delta x_e}"
    r"{\left| u_e \right| + \sqrt{g\,h_e}}"
)

NSE = (
    r"NSE = 1 - \frac{\sum_{i=1}^{n} (o_i - s_i)^2}"
    r"{\sum_{i=1}^{n} (o_i - \bar{o})^2}"
)

CORPUS = [
    BOUSSINESQ,
    CFL,
    NSE,
    r"\hat{Q}_{\max} \ge \bar{y} + \vec{v} - \overline{x}",
    r"\int_0^\infty e^{-x}\,dx = \sqrt{\pi}",
    r"\sum_{i=1}^{n} a_i + \prod_{j=1}^{m} b_j",
    r"\sum_{i=1}^{n} (x_i - \mu)^2 = \sigma^2",
    r"\sum_{i=1}^{n} \| \mathbf{x}_i - \mathbf{y}_i \|_2^2 = 0",
    r"\binom{n}{k} p^k (1-p)^{n-k} = \binom{n}{n-k}",
    r"\iint_D f(x, y)\,dA, \quad \oint_C \mathbf{F} \cdot d\mathbf{s}",
    r"\lim_{n \to \infty} \left( 1 + \tfrac{1}{n} \right)^{n} = e",
    r"\begin{bmatrix} K_{xx} & K_{xy} \\ K_{yx} & K_{yy} \end{bmatrix}",
    r"\mathbf{q}, \quad d\mathbf{s}, \quad \mathsf{x} + \mathcal{l}",
    CASES_LATEX,
    ALIGNED_LATEX,
    r"\operatorname{arg\,min}_{\theta} J(\theta) + \mathrm{NSE}",
]


@pytest.mark.parametrize("latex", CORPUS)
def test_corpus_validates_against_ecma376(latex: str) -> None:
    inline = latex_to_omml(latex, block_id="eq-corpus")
    assert len(inline) > 0
    assert_ecma376(inline)
    assert_ecma376(latex_to_omml(latex, display=True, block_id="eq-corpus"))


def test_boussinesq_structure() -> None:
    root = latex_to_omml(BOUSSINESQ, block_id="eq-boussinesq")
    assert len(_findall(root, ".//m:f")) == 5
    delimiters = _findall(root, "m:d")
    assert len(delimiters) == 1
    assert _tags(list(delimiters[0])) == ["m:dPr", "m:e"]
    assert "\u2202" in _texts(root)  # partial derivatives survived
    assert _texts(root).count("=") == 1


def test_cfl_structure_nests_fraction_radical_delimiter() -> None:
    root = latex_to_omml(CFL, block_id="eq-cfl")
    assert _tags(list(root))[:5] == ["m:r", "m:r", "m:r", "m:limLow", "m:f"]
    fraction = _find(root, "m:f")  # min does not swallow the fraction
    assert fraction is not None
    denominator = _find(fraction, "m:den")
    assert _find(denominator, "m:d") is not None  # \left| u_e \right|
    assert _find(denominator, "m:rad") is not None  # \sqrt{g h_e}
    assert _find(root, "m:limLow/m:e/m:r/m:rPr/m:sty").get(VAL) == "p"


# ---------------------------------------------------------------------------
# Minimal .docx package round trip
# ---------------------------------------------------------------------------

_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType='
    b'"application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" ContentType='
    b'"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    b"</Types>"
)

_RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type='
    b'"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
    b' Target="word/document.xml"/>'
    b"</Relationships>"
)


def test_tracked_replacement_survives_minimal_docx_package(tmp_path) -> None:
    deleted, inserted, _ = build_tracked_omml_replacement(
        OLD_LATEX,
        NEW_LATEX,
        rev_id_start=41,
        author=AUTHOR,
        date_utc=DATE,
        block_id="eq-docx",
    )
    document = etree.Element(W + "document", nsmap={"w": W_NS, "m": M_NS})
    body = etree.SubElement(document, W + "body")
    body.append(_wrap(deleted, inserted))
    document_xml = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    package = tmp_path / "marked.docx"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", document_xml)

    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
        ]
        for name in archive.namelist():
            etree.fromstring(archive.read(name))  # every part is well-formed XML
        parsed = etree.fromstring(archive.read("word/document.xml"))

    assert _find(parsed, ".//w:del") is not None
    assert _find(parsed, ".//w:ins") is not None
    accept_all_revisions(parsed)
    omath = _find(parsed, ".//m:oMath")
    assert omath is not None
    assert _canonical(omath) == _canonical(
        latex_to_omml(NEW_LATEX, block_id="eq-docx")
    )
