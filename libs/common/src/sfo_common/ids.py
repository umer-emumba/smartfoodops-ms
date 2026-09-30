import uuid_utils


def new_id() -> str:
    """UUIDv7 (RFC 9562) — time-ordered, unlike v4's fully random layout.

    Used for every primary/foreign key in every service. The time-ordering
    keeps B-tree index writes append-mostly instead of scattering inserts
    across random pages, which matters under sustained write load. Not used
    for ephemeral values like the request correlation ID (sfo_common.middleware)
    since those are never DB keys and gain nothing from being sortable.
    See docs/technical-documentation.md for the full rationale.
    """
    return str(uuid_utils.uuid7())
