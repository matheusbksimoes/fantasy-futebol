from django.core.management.base import BaseCommand
from league.models import Draft, DraftPick


class Command(BaseCommand):
    help = "Inicia o draft marcando o primeiro pick como atual"

    def add_arguments(self, parser):
        parser.add_argument("--draft_id", type=int, required=True)

    def handle(self, *args, **options):
        draft_id = options["draft_id"]

        try:
            draft = Draft.objects.get(id=draft_id)
        except Draft.DoesNotExist:
            self.stderr.write(self.style.ERROR("Draft não encontrado"))
            return

        # Limpa qualquer pick atual
        DraftPick.objects.filter(draft=draft).update(is_current=False)

        first_pick = (
            DraftPick.objects
            .filter(draft=draft, player__isnull=True)
            .order_by("overall_number")
            .first()
        )

        if not first_pick:
            self.stdout.write(self.style.WARNING("Draft já finalizado"))
            return

        first_pick.is_current = True
        first_pick.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Draft iniciado: Round {first_pick.round_number}, Pick {first_pick.pick_number}"
            )
        )
