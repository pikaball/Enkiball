"""Math tokens and conservative, copyable LaTeX rendering for the terminal."""

from __future__ import annotations

from functools import lru_cache

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from pylatexenc.latex2text import LatexNodes2Text, SpecialsTextSpec
from pylatexenc.latex2text import get_default_latex_context_db as text_context
from pylatexenc.latexwalker import LatexWalker
from pylatexenc.latexwalker import get_default_latex_context_db as parser_context
from pylatexenc.macrospec import MacroStandardArgsParser, SpecialsSpec


_DELIMITERS = (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$"))
_SUPERSCRIPT = str.maketrans("0123456789+-=()in", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ")
_SUBSCRIPT = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")


def _script_text(node, l2tobj: LatexNodes2Text) -> str:
    value = l2tobj.nodelist_to_text(node.nodeargd.argnlist)
    table = _SUPERSCRIPT if node.specials_chars == "^" else _SUBSCRIPT
    if value and all(ord(char) in table for char in value):
        return value.translate(table)
    # Unicode has only a subset of scripts. Keep grouping explicit otherwise.
    return f"{node.specials_chars}({value})"


class _MathText(LatexNodes2Text):
    def macro_node_to_text(self, node) -> str:
        if node.macroname in {"left", "right"}:
            return ""
        spec = self.latex_context.get_macro_spec(node.macroname)
        if (
            spec is None
            or (spec.discard and not spec.simplify_repl)
            or node.macroname in {"input", "include"}
        ):
            raise ValueError(f"Unsupported macro: {node.macroname}")
        if node.macroname in {"frac", "dfrac", "tfrac"}:
            parts = [self.nodelist_to_text([arg]) for arg in node.nodeargd.argnlist]
            return "/".join(part if len(part) == 1 else f"({part})" for part in parts)
        return super().macro_node_to_text(node)

    def environment_node_to_text(self, node) -> str:
        spec = self.latex_context.get_environment_spec(node.environmentname)
        if spec is None or spec.discard:
            raise ValueError(f"Unsupported environment: {node.environmentname}")
        return super().environment_node_to_text(node)


@lru_cache(maxsize=1)
def _latex_contexts():
    parser = parser_context()
    parser.add_context_category(
        "terminal-scripts",
        specials=[SpecialsSpec(char, MacroStandardArgsParser("{")) for char in "^_"],
        prepend=True,
    )
    text = text_context()
    text.add_context_category(
        "terminal-scripts",
        specials=[SpecialsTextSpec(char, simplify_repl=_script_text) for char in "^_"],
        prepend=True,
    )
    return parser, text


def render_latex(expression: str, source: str) -> str:
    """Return Unicode math, or the entire original span on any conversion failure."""
    try:
        parser, text = _latex_contexts()
        walker = LatexWalker(expression, latex_context=parser, tolerant_parsing=False)
        nodes, _, _ = walker.get_latex_nodes(
            parsing_state=walker.make_parsing_state(in_math_mode=True),
        )
        rendered = _MathText(latex_context=text, strict_latex_spaces=False).nodelist_to_text(nodes)
        return rendered.strip() or source
    except Exception:
        # Math is best-effort presentation; it must never interrupt a response.
        return source


def _closing_position(source: str, closing: str, start: int) -> int:
    while (position := source.find(closing, start)) >= 0:
        back = position
        while back > 0 and source[back - 1] == "\\":
            back -= 1
        if (position - back) % 2 == 0:
            return position
        start = position + len(closing)
    return -1


def _inline_math(state: StateInline, silent: bool) -> bool:
    for opening, closing in _DELIMITERS:
        if state.src.startswith(opening, state.pos):
            break
    else:
        return False
    start = state.pos + len(opening)
    if opening == "$" and (
        start == state.posMax or state.src[start].isspace()
        or (state.pos > 0 and state.src[state.pos - 1].isdigit())
    ):
        return False
    end = _closing_position(state.src[:state.posMax], closing, start)
    if opening == "$" and end < 0 and not any(
        char in state.src[start:state.posMax] for char in "\\_^"
    ):
        return False  # An ordinary currency amount must not consume later Markdown.
    if opening == "$" and end >= 0 and (
        state.src[end - 1].isspace()
        or (end + 1 < state.posMax and state.src[end + 1].isdigit())
    ):
        return False
    stop = end + len(closing) if end >= 0 else state.posMax
    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = state.src[start:end] if end >= 0 else ""
        token.meta = {"source": state.src[state.pos:stop], "closed": end >= 0}
    state.pos = stop
    return True


def _block_math(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    if state.sCount[start] - state.blkIndent >= 4:
        return False
    first = state.src[state.bMarks[start] + state.tShift[start]:state.eMarks[start]]
    for opening, closing in _DELIMITERS[:2]:
        if first.startswith(opening):
            break
    else:
        return False
    if silent:
        return True
    lines = [first]
    position = _closing_position(first, closing, len(opening))
    last = start
    while position < 0 and last + 1 < end:
        if state.sCount[last + 1] < state.blkIndent and not state.isEmpty(last + 1):
            break
        last += 1
        line = state.getLines(last, last + 1, state.blkIndent, False)
        lines.append(line)
        position = _closing_position(line, closing, 0)
    if position >= 0 and lines[-1][position + len(closing):].strip():
        return False  # Inline display math followed by prose.
    source = "\n".join(lines)
    token = state.push("math_block", "math", 0)
    token.block = True
    token.map = [start, last + 1]
    token.content = ""
    if position >= 0:
        # The closing delimiter can have trailing spaces on its source line.
        token.content = source[len(opening):len(source) - len(lines[-1]) + position]
    token.meta = {"source": source, "closed": position >= 0}
    state.line = last + 1
    return True


@lru_cache(maxsize=1)
def markdown_parser() -> MarkdownIt:
    parser = MarkdownIt().enable("strikethrough").enable("table")
    parser.inline.ruler.before("escape", "terminal_math", _inline_math)
    parser.block.ruler.before(
        "fence", "terminal_math", _block_math,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    return parser
