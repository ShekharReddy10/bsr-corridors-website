"""Transferred-out stays (guest sent to another hotel) free their room, like cancelled ones.

Also rebuilds the Postgres no-overlap constraint so it ignores transferred stays.
"""

from django.db import migrations, models


def _constraint(statuses: str) -> str:
    return f"""
ALTER TABLE stays DROP CONSTRAINT IF EXISTS stays_no_overlap;
ALTER TABLE stays ADD CONSTRAINT stays_no_overlap
  EXCLUDE USING gist (room_id WITH =, daterange(check_in, check_out, '[)') WITH &&)
  WHERE (status NOT IN ({statuses}));
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_constraint("'cancelled', 'transferred'"))


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_constraint("'cancelled'"))


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_open_ended_and_quick_bookings'),
    ]

    operations = [
        migrations.AddField(
            model_name='stay',
            name='transfer_reason',
            field=models.CharField(blank=True, max_length=200, verbose_name='Reason for transfer'),
        ),
        migrations.AddField(
            model_name='stay',
            name='transferred_to',
            field=models.CharField(blank=True, max_length=120, verbose_name='Transferred to (hotel)'),
        ),
        migrations.AlterField(
            model_name='stay',
            name='status',
            field=models.CharField(choices=[('upcoming', 'Upcoming'), ('checked_in', 'Checked in'), ('checked_out', 'Checked out'), ('cancelled', 'Cancelled'), ('transferred', 'Transferred out')], db_index=True, default='upcoming', max_length=20),
        ),
        migrations.RunPython(forwards, backwards),
    ]
