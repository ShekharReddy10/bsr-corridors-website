import django.db.models.deletion
from django.db import migrations, models


def link_to_guest(apps, schema_editor):
    """Photos so far were added on a booking; they now belong to that booking's guest."""
    IdPhoto = apps.get_model("core", "IdPhoto")
    for p in IdPhoto.objects.select_related("stay"):
        p.guest_id = p.stay.guest_id
        p.save(update_fields=["guest"])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_id_photos'),
    ]

    operations = [
        migrations.AddField(
            model_name='idphoto',
            name='guest',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='id_photos', to='core.guest'),
        ),
        migrations.RunPython(link_to_guest, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='idphoto',
            name='guest',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='id_photos', to='core.guest'),
        ),
        migrations.AlterField(
            model_name='idphoto',
            name='stay',
            field=models.ForeignKey(blank=True, help_text='The booking it was added on (used in the file name)', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='core.stay'),
        ),
    ]
