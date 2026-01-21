from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from league.models import League, Draft, DraftPick, Team


class Command(BaseCommand):
    help = "Gera a board do draft (picks) para uma liga. Ex: python manage.py generate_draft --league_id 1 --rounds 20 --type snake --reset"

    def add_arguments(self, parser):
        parser.add_argument("--league_id", type=int, required=True)
        parser.add_argument("--rounds", type=int, default=20)
        parser.add_argument("--type", type=str, choices=["snake", "linear"], default="snake")
        parser.add_argument("--reset", action="store_true", help="Apaga picks existentes e gera de novo")

    @transaction.atomic
    def handle(self, *args, **options):
        league_id = options["league_id"]
        rounds = options["rounds"]
        draft_type = options["type"]
        reset = options["reset"]

        try:
            league = League.objects.get(id=league_id)
        except League.DoesNotExist:
            raise CommandError(f"League id={league_id} não existe.")

        teams = list(Team.objects.filter(league=league).order_by("id"))
        if len(teams) == 0:
            raise CommandError("Essa liga não tem times. Crie os times primeiro no admin.")
        if len(teams) != 10:
            self.stdout.write(self.style.WARNING(f"A liga tem {len(teams)} times (o alvo é 10). Pode continuar, mas confira."))

        draft, created = Draft.objects.get_or_create(
            league=league,
            defaults={"draft_type": draft_type, "rounds": rounds},
        )

        if not created:
            draft.draft_type = draft_type
            draft.rounds = rounds
            draft.save()

        if reset:
            DraftPick.objects.filter(draft=draft).delete()

        if DraftPick.objects.filter(draft=draft).exists() and not reset:
            raise CommandError("Já existem picks para esse draft. Use --reset para gerar novamente.")

        num_teams = len(teams)
        overall = 1

        for r in range(1, rounds + 1):
            order = list(reversed(teams)) if (draft_type == "snake" and r % 2 == 0) else teams

            for i, team in enumerate(order, start=1):
                DraftPick.objects.create(
                    draft=draft,
                    round_number=r,
                    pick_number=i,
                    overall_number=overall,
                    team=team,
                )
                overall += 1

        self.stdout.write(self.style.SUCCESS(
            f"Draft gerado: league={league.id} teams={num_teams} rounds={rounds} type={draft_type} picks={overall-1}"
        ))
