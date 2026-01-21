from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import DraftPick, RosterSpot


class Command(BaseCommand):
    help = "Cria RosterSpot para todos os DraftPicks já preenchidos (player != null)."

    def add_arguments(self, parser):
        parser.add_argument("--draft_id", type=int, default=1)

    @transaction.atomic
    def handle(self, *args, **options):
        draft_id = options["draft_id"]

        picks = (
            DraftPick.objects
            .select_related("draft", "team", "player")
            .filter(draft_id=draft_id, player__isnull=False)
            .order_by("overall_number")
        )

        created = 0
        skipped = 0

        for p in picks:
            # UniqueConstraint (draft, player) impede duplicata
            obj, was_created = RosterSpot.objects.get_or_create(
                draft=p.draft,
                player=p.player,
                defaults={
                    "team": p.team,
                    "acquired_via": "DRAFT",
                }
            )
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Sync concluído. Criados: {created} | Já existiam: {skipped}"
        ))
