from django.core.exceptions import ValidationError
from django.db import transaction

from league.models import LineupSlot, FORMATION_MAP


def ensure_slots_for_formation(lineup):
    if lineup.formation not in FORMATION_MAP:
        raise ValidationError("Formação inválida.")

    req = FORMATION_MAP[lineup.formation]

    def ensure(slot_type, count):
        existing = list(
            lineup.slots.filter(slot_type=slot_type).order_by("slot_index")
        )

        # cria slots faltantes
        for i in range(len(existing) + 1, count + 1):
            LineupSlot.objects.create(
                lineup=lineup,
                slot_type=slot_type,
                slot_index=i,
            )

        # remove slots excedentes (somente se vazios)
        if len(existing) > count:
            for s in existing[count:]:
                if s.player is not None:
                    raise ValidationError(
                        f"Não é possível reduzir {slot_type}: slot {slot_type}{s.slot_index} já está ocupado."
                    )
                s.delete()

    with transaction.atomic():
        ensure("GOL", 1)
        ensure("TEC", 1)
        ensure("ZAG", req["ZAG"])
        ensure("LAT", req["LAT"])
        ensure("MEI", req["MEI"])
        ensure("ATA", req["ATA"])
