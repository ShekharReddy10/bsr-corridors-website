"""Postgres: one resident per bed at a time — two spells in the same bed can't overlap.

The leaving day is excluded ('[)'), so one resident can move out and the next move in on the same day. An open spell
(no end yet) runs on for ever. A cancelled reservation's spell is closed on its own start day (empty range), so it
never blocks the bed.
"""

from django.db import migrations

CREATE = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE pg_bedspell ADD CONSTRAINT pg_bedspell_no_overlap
  EXCLUDE USING gist (bed_id WITH =, daterange(start, "end", '[)') WITH &&);
"""
DROP = "ALTER TABLE pg_bedspell DROP CONSTRAINT IF EXISTS pg_bedspell_no_overlap;"


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP)


class Migration(migrations.Migration):
    dependencies = [("pg", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
