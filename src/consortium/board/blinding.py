"""Blinding boundary (AD-2) -- stub.

Only this module reads or writes ``blinding_key.csv``, the one place where
Conditions exist. Only ``consortium.stages.push``, ``consortium.stages.export``
and ``consortium.board`` itself may import it; ``tests/test_architecture.py``
enforces that. Conditions never enter ``board.db``, the Archive, logs or any
request.
"""
