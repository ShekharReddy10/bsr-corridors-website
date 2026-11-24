"""Default WhatsApp messages for residents, and advances already recorded at move-in become advance payments (so the
advance is the sum of its payments from now on). Runs only in a PG / co-living property's own tables."""

from django.db import migrations

from pg.defaults import MESSAGES  # noqa: E402  (plain data, safe to import in a migration)


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    PgMessage = apps.get_model("pg", "PgMessage")
    for i, (name, use, body) in enumerate(MESSAGES):
        if not PgMessage.objects.using(db).filter(use=use).exists():
            PgMessage.objects.using(db).create(name=name, use=use, body=body, sort_order=i)
    Tenancy = apps.get_model("pg", "Tenancy")
    RentPayment = apps.get_model("pg", "RentPayment")
    for t in Tenancy.objects.using(db).filter(deposit_received__gt=0):
        if not RentPayment.objects.using(db).filter(tenancy=t, kind="deposit").exists():
            RentPayment.objects.using(db).create(tenancy=t, kind="deposit", amount=t.deposit_received,
                                                 paid_on=t.move_in, mode="other", note="Recorded at move-in")


class Migration(migrations.Migration):
    dependencies = [("pg", "0004_rent_and_money")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
