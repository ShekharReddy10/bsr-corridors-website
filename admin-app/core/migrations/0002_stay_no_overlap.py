"""Postgres: make overlapping (non-cancelled) stays in the same room impossible.

Uses an exclusion constraint on (room, date range). Check-out day is excluded ('[)'),
so one guest can leave and the next arrive on the same day. Skipped on SQLite.
"""

from django.db import migrations

CREATE = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE core_stay ADD CONSTRAINT core_stay_no_overlap
  EXCLUDE USING gist (room_id WITH =, daterange(check_in, check_out, '[)') WITH &&)
  WHERE (status <> 'cancelled');
"""
DROP = "ALTER TABLE core_stay DROP CONSTRAINT IF EXISTS core_stay_no_overlap;"  # renamed to stays_no_overlap in 0003


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
