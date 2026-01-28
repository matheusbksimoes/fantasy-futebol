import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from league.models import Draft, Week, Player, PlayerWeekScore

CARTOLA_BASE = "https://api.cartola.globo.com"


class Command(BaseCommand):
    help = "Sincroniza pontuações do Cartola para a Week atual (ou Week informada)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--draft-id",
            type=int,
            default=None,
            help="ID do Draft (default: último draft)",
        )
        parser.add_argument(
            "--week",
            type=int,
            default=None,
            help="Número da Week (default: week marcada como current)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=15,
            help="Timeout HTTP em segundos",
        )

    def handle(self, *args, **options):
        draft_id = options["draft_id"]
        week_number = options["week"]
        timeout = options["timeout"]

        # -----------------------------
        # Draft
        # -----------------------------
        draft = (
            Draft.objects.filter(id=draft_id).first()
            if draft_id
            else Draft.objects.order_by("-id").first()
        )

        if not draft:
            self.stderr.write(self.style.ERROR("Nenhum draft encontrado."))
            return

        # -----------------------------
        # Week
        # -----------------------------
        if week_number:
            week = Week.objects.filter(draft=draft, number=week_number).first()
        else:
            week = Week.objects.filter(draft=draft, is_current=True).first()

        if not week:
            self.stderr.write(
                self.style.ERROR("Nenhuma Week encontrada (crie uma Week e marque is_current=True).")
            )
            return

        # -----------------------------
        # Status do mercado
        # -----------------------------
        try:
            status = requests.get(
                f"{CARTOLA_BASE}/mercado/status",
                timeout=timeout,
            ).json()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar mercado/status: {e}"))
            return

        rodada_atual = status.get("rodada_atual")
        self.stdout.write(
            self.style.SUCCESS(
                f"Cartola rodada_atual={rodada_atual} | salvando em Draft={draft.id}, Week={week.number}"
            )
        )

        # -----------------------------
        # Pontuações dos atletas
        # -----------------------------
        try:
            payload = requests.get(
                f"{CARTOLA_BASE}/atletas/pontuados",
                timeout=timeout,
            ).json()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar atletas/pontuados: {e}"))
            return

        atletas = payload.get("atletas") or {}
        if not atletas:
            self.stdout.write(
                self.style.WARNING(
                    "Nenhum atleta pontuado retornado (rodada pode não ter iniciado)."
                )
            )
            return

        # -----------------------------
        # Jogadores do seu banco
        # -----------------------------
        players = (
            Player.objects
            .exclude(cartola_id__isnull=True)
            .exclude(cartola_id__exact="")
        )

        upserts = 0
        missing = 0

        with transaction.atomic():
            for player in players:
                atleta_id = str(player.cartola_id)
                data = atletas.get(atleta_id)

                if not data:
                    missing += 1
                    continue

                points = data.get("pontuacao", 0) or 0
                scouts = data.get("scout", {}) or {}

                PlayerWeekScore.objects.update_or_create(
                    week=week,
                    player=player,
                    defaults={
                        "points": points,
                        "scouts": scouts,
                        "source": "CARTOLA",
                        "fetched_at": timezone.now(),
                    },
                )

                upserts += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sincronização concluída: {upserts} atualizados | {missing} sem pontuação"
            )
        )
