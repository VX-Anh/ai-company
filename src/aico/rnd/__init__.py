"""Phòng R&D: chỉ mục kho tri thức, tool knowledge, khối <knowledge> cho prompt (GĐ2)."""

from aico.rnd.cards import Card, CardError, cards_from_archive, fold, parse_card
from aico.rnd.librarian import LIBRARIAN_ROLE, Librarian
from aico.rnd.mcp import build_knowledge_server
from aico.rnd.store import KNOWLEDGE_ROLES, KnowledgeStore, query_terms

__all__ = [
    "KNOWLEDGE_ROLES",
    "LIBRARIAN_ROLE",
    "Card",
    "CardError",
    "KnowledgeStore",
    "Librarian",
    "build_knowledge_server",
    "cards_from_archive",
    "fold",
    "parse_card",
    "query_terms",
]
