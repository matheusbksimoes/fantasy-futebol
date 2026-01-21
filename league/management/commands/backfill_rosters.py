from django.core.management.base import BaseCommand
from league.models import DraftPick, Roster


class Command(BaseCommand):
    help = "Cria Roster para todos os DraftPicks que já têm player (backfill)."

    def add_arguments(self, parser):
        parser.add_argument("--draft_id", type=int, required=False, help="Opcional: filtra por um Draft específico")

    def handle(self, *args, **options):
        draft_id = options.get("draft_id")

        qs = DraftPick.objects.filter(player__isnull=False)
        if draft_id:
            qs = qs.filter(draft_id=draft_id)

        created_count = 0
        total = qs.count()

        for pick in qs.select_related("team", "player"):
            _, created = Roster.objects.get_or_create(
                team=pick.team,
                player=pick.player,
                defaults={"active": True},
            )
            if created:
                created_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Backfill concluído. DraftPicks com player: {total} | Rosters criados agora: {created_count}"
        ))
