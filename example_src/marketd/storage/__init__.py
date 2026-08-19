"""Persistence.

The backing store here is a dict, but the *shape* is the shape you want in
front of a real database: repositories that hide the storage, a query object
that is built and compiled once, optimistic versioning instead of locks, and an
append-only log that downstream consumers tail.  Swapping the dict for SQL
touches these files and nothing above them.
"""
