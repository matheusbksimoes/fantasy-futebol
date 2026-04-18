# league/management/commands/fix_roster_order.py
from django.core.management.base import BaseCommand
from league.models import RosterSpot


class Command(BaseCommand):
    help = "Preenche manual_order dos roster spots ativos"

    def handle(self, *args, **options):
        team_keys = (
            RosterSpot.objects
            .filter(dropped_at__isnull=True)
            .values_list("draft_id", "team_id")
            .distinct()
        )

        updated_total = 0

        for draft_id, team_id in team_keys:
            spots = list(
                RosterSpot.objects
                .filter(draft_id=draft_id, team_id=team_id, dropped_at__isnull=True)
                .select_related("player")
                .order_by("player__position", "player__name", "id")
            )

            for idx, spot in enumerate(spots):
                spot.manual_order = idx

            RosterSpot.objects.bulk_update(spots, ["manual_order"])
            updated_total += len(spots)

        self.stdout.write(self.style.SUCCESS(
            f"manual_order preenchido com sucesso para {updated_total} roster spots."
        ))