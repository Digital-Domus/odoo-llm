def pre_init_hook(cr):
    """Pre-create user_vote column with default 0 to avoid computation on install for existing records."""
    cr.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'mail_message' AND column_name = 'user_vote'"""
    )
    if not cr.fetchone():
        cr.execute(
            "ALTER TABLE mail_message ADD COLUMN user_vote int4 DEFAULT 0 NOT NULL"
        )
