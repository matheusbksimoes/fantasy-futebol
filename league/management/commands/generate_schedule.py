from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import Draft, Team, Week, Matchup


def round_robin_pairs(teams):
    """
    Retorna uma lista de rodadas; cada rodada é uma lista de pares (home, away).
    Método do círculo. Exige número PAR de times.
    """
    n = len(teams)
    fixed = teams[0]
    rotating = teams[1:]
    rounds = []

    total_rounds = n - 1
    for _ in range(total_rounds):
        left = [fixed] + rotating[: (n // 2) - 1]
        right = list(reversed(rotating[(n // 2) - 1 :]))
        rounds.append(list(zip(left, right)))
        rotating = rotating[1:] + rotating[:1]

    return rounds


class Command(BaseCommand):
    help = "Gera schedule para temporada regular (27 rodadas = round-robin triplo) para um draft."

    def add_arguments(self, parser):
        parser.add_argument("--draft-id", type=int, required=True)

    def handle(self, *args, **options):
        draft_id = options["draft_id"]
        draft = Draft.objects.filter(id=draft_id).first()
        if not draft:
            self.stderr.write(self.style.ERROR("Draft não encontrado."))
            return

        teams = list(Team.objects.filter(league=draft.league).order_by("id"))
        n = len(teams)

        if n % 2 != 0:
            self.stderr.write(self.style.ERROR("Número de times precisa ser PAR (ex: 10)."))
            return

        base_rounds = round_robin_pairs(teams)  # 9 rodadas
        total_weeks = 27  # 3 turnos

        self.stdout.write(self.style.SUCCESS(f"Gerando schedule: {n} times | {total_weeks} rodadas"))

        with transaction.atomic():
            # limpar weeks/matchups anteriores do draft
            week_ids = list(Week.objects.filter(draft=draft).values_list("id", flat=True))
            Matchup.objects.filter(week_id__in=week_ids).delete()
            Week.objects.filter(draft=draft).delete()

            week_number = 1

            # Turno 1: base (A x B)
            for pairs in base_rounds:
                week = Week.objects.create(draft=draft, number=week_number, is_current=(week_number == 1))
                for home, away in pairs:
                    Matchup.objects.create(week=week, home_team=home, away_team=away)
                week_number += 1

            # Turno 2: invertido (B x A)
            for pairs in base_rounds:
                week = Week.objects.create(draft=draft, number=week_number, is_current=False)
                for home, away in pairs:
                    Matchup.objects.create(week=week, home_team=away, away_team=home)
                week_number += 1

            # Turno 3: base de novo (A x B)
            for pairs in base_rounds:
                week = Week.objects.create(draft=draft, number=week_number, is_current=False)
                for home, away in pairs:
                    Matchup.objects.create(week=week, home_team=home, away_team=away)
                week_number += 1

        self.stdout.write(self.style.SUCCESS("OK! Weeks 1–27 e matchups gerados."))
