import requests
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum
from django.db.models.functions import Coalesce

from league.models import Draft, Week, Player, PlayerWeekScore, Matchup, LineupSpot

CARTOLA_BASE = "https://api.cartola.globo.com"


def compute_team_points(week, team):
    # Soma pontos dos jogadores escalados do time na semana (do jeito mais seguro)
    player_ids = (
        LineupSpot.objects
        .filter(lineup__week=week, lineup__team=team)
        .exclude(player__isnull=True)
        .values_list("player_id", flat=True)
        .distinct()
    )

    return (
        PlayerWeekScore.objects
        .filter(week=week, player_id__in=player_ids)
        .aggregate(total=Coalesce(Sum("points"), Decimal("0")))["total"]
    )


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

    # -----------------------------
    # Helpers
    # -----------------------------
    def _get_json_or_none(self, url: str, timeout: int):
        """
        Retorna:
          - dict (JSON) quando houver JSON válido
          - None quando status 204 ou corpo vazio
        Lança Exception com mensagem útil quando status inesperado ou resposta não-JSON.
        """
        r = requests.get(url, timeout=timeout)

        # 204: sem conteúdo -> não é erro (apenas nada a importar)
        if r.status_code == 204:
            return None

        # Qualquer coisa diferente de 200/204 é erro
        if r.status_code != 200:
            snippet = (r.text or "")[:200].replace("\n", " ")
            raise Exception(f"HTTP {r.status_code} em {url}. Body: {snippet}")

        # 200 mas sem corpo (raro) -> trate como None
        if not r.content or not (r.text or "").strip():
            return None

        # Tenta parsear JSON com fallback de erro mais legível
        try:
            return r.json()
        except ValueError:
            snippet = (r.text or "")[:200].replace("\n", " ")
            raise Exception(f"Resposta 200 não-JSON em {url}. Body: {snippet}")

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
                self.style.ERROR(
                    "Nenhuma Week encontrada (crie uma Week e marque is_current=True)."
                )
            )
            return

        # -----------------------------
        # Status do mercado
        # -----------------------------
        try:
            status = self._get_json_or_none(
                f"{CARTOLA_BASE}/mercado/status",
                timeout=timeout,
            )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar mercado/status: {e}"))
            return

        if not status:
            self.stderr.write(
                self.style.ERROR("mercado/status retornou vazio (inesperado).")
            )
            return

        rodada_atual = status.get("rodada_atual")
        self.stdout.write(
            self.style.SUCCESS(
                f"Cartola rodada_atual={rodada_atual} | salvando em Draft={draft.id}, Week={week.number}"
            )
        )

        # ✅ REGRA DO CRON: só executa se a rodada do Cartola bater com a Week do app
        if rodada_atual and week.number != rodada_atual:
            self.stdout.write(
                self.style.WARNING(
                    f"Pulando: Week do app ({week.number}) != rodada_atual do Cartola ({rodada_atual})."
                )
            )
            return

        # -----------------------------
        # Pontuações dos atletas
        # -----------------------------
        try:
            payload = self._get_json_or_none(
                f"{CARTOLA_BASE}/atletas/pontuados",
                timeout=timeout,
            )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar atletas/pontuados: {e}"))
            return

        # 204 (ou corpo vazio): nada a importar agora
        if payload is None:
            self.stdout.write(
                self.style.WARNING(
                    "atletas/pontuados retornou 204 (sem pontuação disponível agora). "
                    "Isso é normal fora de rodada/parciais."
                )
            )
            return

        atletas = payload.get("atletas") or {}
        if not atletas:
            self.stdout.write(
                self.style.WARNING(
                    "Nenhum atleta pontuado retornado (rodada pode não ter iniciado / sem parcial)."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"Cartola retornou {len(atletas)} atletas pontuados.")
        )

        # -----------------------------
        # Jogadores do seu banco
        # -----------------------------
        # ✅ FIX: cartola_id é IntegerField -> NÃO comparar com string vazia.
        players = Player.objects.exclude(cartola_id__isnull=True)

        # (opcional) pra depurar rapidamente se o mapping está batendo:
        try:
            sample_atleta_id = next(iter(atletas.keys()))
            sample_points = (atletas.get(sample_atleta_id) or {}).get("pontuacao")
            self.stdout.write(
                f"Amostra retorno: atleta_id={sample_atleta_id} pontuacao={sample_points}"
            )
        except StopIteration:
            pass

        upserts = 0
        missing = 0

        # Para performance e para não sofrer com dados ruins, mapeia por cartola_id
        players_by_cartola_id = {
            str(p.cartola_id): p for p in players if p.cartola_id is not None
        }

        with transaction.atomic():
            for atleta_id, data in atletas.items():
                player = players_by_cartola_id.get(str(atleta_id))
                if not player:
                    missing += 1
                    continue

                points = (data or {}).get("pontuacao", 0) or 0
                scouts = (data or {}).get("scout", {}) or {}

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
                f"Sincronização concluída: {upserts} upserts | {missing} atletas no payload sem player correspondente no banco"
            )
        )

        # -----------------------------
        # Atualizar placar dos matchups da Week
        # -----------------------------
        matchups = Matchup.objects.filter(week=week).select_related("home_team", "away_team")

        updated_matchups = 0
        for m in matchups:
            home_pts = compute_team_points(week, m.home_team)
            away_pts = compute_team_points(week, m.away_team)

            # Só salva se mudou
            if m.home_score != home_pts or m.away_score != away_pts:
                m.home_score = home_pts
                m.away_score = away_pts
                m.save(update_fields=["home_score", "away_score"])
                updated_matchups += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Placar atualizado: {updated_matchups}/{matchups.count()} matchups na Week {week.number}"
            )
        )
