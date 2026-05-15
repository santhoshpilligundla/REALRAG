"""tree-sitter TypeScript parser. Extracts atomic units for Angular UI repos.

Atomic units:
  - ts_class           — class declaration
  - ts_interface       — interface declaration
  - ts_type_alias      — type alias declaration
  - ts_function        — top-level function
  - ts_method          — method inside a class
  - ts_component       — class with @Component decorator (Angular)
  - ts_service         — class with @Injectable decorator (Angular)
  - ts_pipe            — class with @Pipe decorator
  - ts_directive       — class with @Directive decorator
  - ts_module          — class with @NgModule decorator

Decorators are detected as part of the parent class definition; the kind
is upgraded from 'ts_class' to 'ts_component'/'ts_service'/etc. accordingly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import tree_sitter
import tree_sitter_typescript

from lib.parsers_common import ParsedEntity


@lru_cache(maxsize=1)
def _parser_ts() -> tree_sitter.Parser:
    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_typescript.language_typescript()))


@lru_cache(maxsize=1)
def _parser_tsx() -> tree_sitter.Parser:
    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_typescript.language_tsx()))


_DECORATOR_TO_KIND = {
    "Component": "ts_component",
    "Injectable": "ts_service",
    "Pipe": "ts_pipe",
    "Directive": "ts_directive",
    "NgModule": "ts_module",
}


def _node_text(src: bytes, node: tree_sitter.Node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_named(node: tree_sitter.Node, types: tuple[str, ...]) -> tree_sitter.Node | None:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _name_of(src: bytes, node: tree_sitter.Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(src, name_node)
    ident = _find_named(node, ("type_identifier", "identifier", "property_identifier"))
    return _node_text(src, ident) if ident else "<anonymous>"


def _decorators_for(src: bytes, class_node: tree_sitter.Node) -> list[str]:
    """Return the list of decorator names attached to a class declaration.

    In tree-sitter-typescript the decorators precede the class node as siblings
    (under a parent like 'export_statement' or 'program'). We search the parent's
    children up to and including class_node.
    """
    decorators: list[str] = []
    parent = class_node.parent
    if parent is None:
        return decorators
    for sibling in parent.children:
        if sibling == class_node:
            break
        if sibling.type == "decorator":
            text = _node_text(src, sibling).lstrip("@").strip()
            head = text.split("(", 1)[0].split(" ", 1)[0]
            if head:
                decorators.append(head)
    return decorators


def _signature_first_line(body: str) -> str:
    first = body.lstrip().splitlines()[0] if body.strip() else ""
    return first.strip()[:240]


def _walk_class(src: bytes, node: tree_sitter.Node) -> ParsedEntity:
    name = _name_of(src, node)
    body = _node_text(src, node)

    decos = _decorators_for(src, node)
    kind = "ts_class"
    for d in decos:
        if d in _DECORATOR_TO_KIND:
            kind = _DECORATOR_TO_KIND[d]
            break

    entity = ParsedEntity(
        kind=kind,
        name=name,
        qualified_name=name,
        signature=_signature_first_line(body),
        body=body,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )

    body_node = node.child_by_field_name("body") or _find_named(node, ("class_body",))
    if body_node is None:
        return entity

    for member in body_node.children:
        if member.type in ("method_definition", "method_signature"):
            mname = _name_of(src, member)
            mbody = _node_text(src, member)
            entity.children.append(ParsedEntity(
                kind="ts_method",
                name=mname,
                qualified_name=f"{name}.{mname}",
                signature=_signature_first_line(mbody),
                body=mbody,
                start_line=member.start_point[0] + 1,
                end_line=member.end_point[0] + 1,
            ))

    return entity


def _walk_interface(src: bytes, node: tree_sitter.Node) -> ParsedEntity:
    name = _name_of(src, node)
    body = _node_text(src, node)
    return ParsedEntity(
        kind="ts_interface",
        name=name,
        qualified_name=name,
        signature=_signature_first_line(body),
        body=body,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _walk_type_alias(src: bytes, node: tree_sitter.Node) -> ParsedEntity:
    name = _name_of(src, node)
    body = _node_text(src, node)
    return ParsedEntity(
        kind="ts_type_alias",
        name=name,
        qualified_name=name,
        signature=_signature_first_line(body),
        body=body,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _walk_function(src: bytes, node: tree_sitter.Node) -> ParsedEntity:
    name = _name_of(src, node)
    body = _node_text(src, node)
    return ParsedEntity(
        kind="ts_function",
        name=name,
        qualified_name=name,
        signature=_signature_first_line(body),
        body=body,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def parse_typescript_file(path: Path) -> list[ParsedEntity]:
    try:
        src = path.read_bytes()
    except OSError:
        return []
    if not src:
        return []

    parser = _parser_tsx() if path.suffix.lower() == ".tsx" else _parser_ts()
    tree = parser.parse(src)
    root = tree.root_node

    out: list[ParsedEntity] = []

    def _visit(node: tree_sitter.Node) -> None:
        for child in node.children:
            t = child.type
            if t == "class_declaration":
                out.append(_walk_class(src, child))
            elif t == "interface_declaration":
                out.append(_walk_interface(src, child))
            elif t == "type_alias_declaration":
                out.append(_walk_type_alias(src, child))
            elif t == "function_declaration":
                out.append(_walk_function(src, child))
            elif t == "export_statement":
                _visit(child)

    _visit(root)
    return out
