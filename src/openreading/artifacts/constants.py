"""Shared wire limits keep constructors, retrieval, and advertised tool inputs aligned."""

MAX_PASSAGE_CHARS = 1024
MAX_QUERY_CHARS = 256
MAX_EXCERPT_CHARS = 240
MAX_SEARCH_HITS = 10
MAX_READ_PASSAGES = 8
MAX_CURSOR_CHARS = 512

# Cursors bind this value, so change it whenever tokenization or ranking can change a hit list.
RETRIEVER_REVISION = "lexical-v4-unicode-wraps"
