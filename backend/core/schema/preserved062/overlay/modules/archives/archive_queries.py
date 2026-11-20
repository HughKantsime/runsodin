"""Archive list filters shared by archive reads and exports."""


def build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag):
    """Build WHERE clause for archive queries."""
    if printer_id is not None:
        conditions.append("a.printer_id = :printer_id")
        params["printer_id"] = printer_id
    if status:
        conditions.append("a.status = :status")
        params["status"] = status
    if search:
        conditions.append("a.print_name LIKE :search")
        params["search"] = f"%{search}%"
    if start_date:
        conditions.append("a.completed_at >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("a.completed_at <= :end_date")
        params["end_date"] = end_date
    if user_id is not None:
        conditions.append("a.user_id = :user_id")
        params["user_id"] = user_id
    if tag:
        # Match comma-separated tag field (exact tag match, not substring)
        conditions.append(
            "(a.tags = :tag_exact OR a.tags LIKE :tag_start "
            "OR a.tags LIKE :tag_mid OR a.tags LIKE :tag_end)"
        )
        params["tag_exact"] = tag
        params["tag_start"] = f"{tag},%"
        params["tag_mid"] = f"%, {tag},%"
        params["tag_end"] = f"%, {tag}"

