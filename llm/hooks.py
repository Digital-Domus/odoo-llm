def pre_init_hook(cr):
    """Pre-create llm_role column to avoid computation on install for existing records."""
    cr.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'mail_message' AND column_name = 'llm_role'"""
    )
    if not cr.fetchone():
        cr.execute("""ALTER TABLE mail_message ADD COLUMN llm_role varchar""")
