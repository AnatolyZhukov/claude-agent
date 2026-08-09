"""Everything that touches the sample_superstore database.

`engine` owns the connection (read-only, built lazily); `queries` owns the SQL
behind each tool and returns `ToolResult`s. Nothing above this package builds
SQL or opens a connection of its own — the agent reaches it only through
tools.py's dispatcher.

Deliberately empty of re-exports: with only two modules here, a facade would
just add a second way to import the same name, and callers importing from the
module that actually defines a thing is clearer than a package-level alias.
"""
