"""Repo-watch helpers: static capability inventory, report building, signing and verification.

Used by GROK-WATCHER and CO-ORDINATOR-BOT (docs/watch/). Reads the repository through git
plumbing and ``ast`` only; it never imports or executes repository code.
"""
