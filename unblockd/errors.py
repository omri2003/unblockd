"""The errors every layer raises: the rules (store.py) and the storage connectors alike. store.py re-exports them."""


class TicketError(ValueError):
	"""Bad input from a caller (maps to HTTP 400 / an MCP tool error)."""


class NotFound(TicketError):
	pass


class Conflict(TicketError):
	"""update(expected_rev=) lost a race: the ticket was written after the caller read it (REST 409)."""
