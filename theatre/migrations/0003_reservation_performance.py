import django.db.models.deletion
from django.db import migrations, models


def bind_reservations_to_performances(apps, schema_editor):
    Reservation = apps.get_model("theatre", "Reservation")
    Ticket = apps.get_model("theatre", "Ticket")
    database = schema_editor.connection.alias
    bindings = []
    without_tickets = []
    multiple_performances = []

    for reservation in Reservation.objects.using(database).order_by("pk").iterator():
        performance_ids = list(
            Ticket.objects.using(database)
            .filter(reservation_id=reservation.pk)
            .order_by()
            .values_list("performance_id", flat=True)
            .distinct()
        )
        if not performance_ids:
            without_tickets.append(reservation.pk)
        elif len(performance_ids) > 1:
            multiple_performances.append(reservation.pk)
        else:
            bindings.append((reservation.pk, performance_ids[0]))

    if without_tickets or multiple_performances:
        raise RuntimeError(
            "Cannot infer Reservation.performance safely. "
            f"Reservations without tickets: {without_tickets}; "
            f"reservations with multiple performances: {multiple_performances}. "
            "Resolve these records explicitly before retrying the migration."
        )

    for reservation_id, performance_id in bindings:
        Reservation.objects.using(database).filter(pk=reservation_id).update(
            performance_id=performance_id
        )


class Migration(migrations.Migration):
    dependencies = [
        ("theatre", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="performance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reservations",
                to="theatre.performance",
            ),
        ),
        # On reversal the field becomes nullable and is then removed;
        # the original Ticket.performance values remain untouched.
        migrations.RunPython(bind_reservations_to_performances, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="reservation",
            name="performance",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reservations",
                to="theatre.performance",
            ),
        ),
    ]
