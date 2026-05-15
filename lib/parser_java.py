"""Tree-sitter based Java parser. Extracts classes, interfaces, enums, methods, constructors."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import tree_sitter
import tree_sitter_java


@dataclass
class JavaEntity:
    kind: str  # 'class' | 'interface' | 'enum' | 'method' | 'constructor'
    name: str
    qualified_name: str
    signature: str
    body: str
    body_hash: str
    start_line: int
    end_line: int
    children: list["JavaEntity"] = field(default_factory=list)


_CONTAINER_KINDS = {"class_declaration", "interface_declaration", "enum_declaration"}
_METHOD_KINDS = {"method_declaration", "constructor_declaration"}


@lru_cache(maxsize=1)
def _parser() -> tree_sitter.Parser:
    lang = tree_sitter.Language(tree_sitter_java.language())
    return tree_sitter.Parser(lang)


def _node_text(src: bytes, node: tree_sitter.Node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _named_child(node: tree_sitter.Node, child_type: str) -> tree_sitter.Node | None:
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _get_name(src: bytes, node: tree_sitter.Node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(src, name_node)
    ident = _named_child(node, "identifier")
    return _node_text(src, ident) if ident else "<anonymous>"


def _hash_body(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()


def _signature_first_line(body: str) -> str:
    first = body.lstrip().splitlines()[0] if body.strip() else ""
    return first.strip()[:240]


def _extract_package(src: bytes, root: tree_sitter.Node) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            scoped = child.child_by_field_name("name")
            if scoped is not None:
                return _node_text(src, scoped)
    return ""


def _walk_container(
    src: bytes,
    node: tree_sitter.Node,
    parent_qname: str,
) -> JavaEntity:
    name = _get_name(src, node)
    qname = f"{parent_qname}.{name}" if parent_qname else name
    body = _node_text(src, node)

    container_kind = {
        "class_declaration":     "class",
        "interface_declaration": "interface",
        "enum_declaration":      "enum",
        "record_declaration":    "record",
    }.get(node.type, "type")

    entity = JavaEntity(
        kind=container_kind,
        name=name,
        qualified_name=qname,
        signature=_signature_first_line(body),
        body=body,
        body_hash=_hash_body(body),
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )

    body_node = node.child_by_field_name("body")
    if body_node is None:
        return entity

    for member in body_node.children:
        if member.type in _CONTAINER_KINDS or member.type == "record_declaration":
            entity.children.append(_walk_container(src, member, qname))
        elif member.type in _METHOD_KINDS:
            mname = _get_name(src, member)
            mbody = _node_text(src, member)
            mkind = "constructor" if member.type == "constructor_declaration" else "method"
            entity.children.append(JavaEntity(
                kind=mkind,
                name=mname,
                qualified_name=f"{qname}.{mname}",
                signature=_signature_first_line(mbody),
                body=mbody,
                body_hash=_hash_body(mbody),
                start_line=member.start_point[0] + 1,
                end_line=member.end_point[0] + 1,
            ))

    return entity


def parse_java_file(path: Path) -> list[JavaEntity]:
    """Return top-level entities in a Java file (each with nested members)."""
    try:
        src = path.read_bytes()
    except OSError:
        return []

    tree = _parser().parse(src)
    root = tree.root_node

    package = _extract_package(src, root)
    top_level: list[JavaEntity] = []

    for child in root.children:
        if child.type in _CONTAINER_KINDS or child.type == "record_declaration":
            top_level.append(_walk_container(src, child, package))

    return top_level


def flatten(entities: list[JavaEntity]) -> list[JavaEntity]:
    out: list[JavaEntity] = []
    stack = list(entities)
    while stack:
        e = stack.pop()
        out.append(e)
        stack.extend(e.children)
    return out
