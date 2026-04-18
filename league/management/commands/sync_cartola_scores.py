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

    def _get_json_or_none(self, url: str, timeout: int):
        r = requests.get(url, timeout=timeout)

        if r.status_code == 204:
            return None

        if r.status_code != 200:
            snippet = (r.text or "")[:200].replace("\n", " ")
            raise Exception(f"HTTP {r.status_code} em {url}. Body: {snippet}")

        if not r.content or not (r.text or "").strip():
            return None

        try:
            return r.json()
        except ValueError:
            snippet = (r.text or "")[:200].replace("\n", " ")
            raise Exception(f"Resposta 200 não-JSON em {url}. Body: {snippet}")

    def _build_match_map(self, rodada: int, timeout: int):
        """
        Retorna um mapa por clube_id com:
        {
            clube_id: {
                "opponent": "FLU",
                "is_home": True,
                "match_display": "SAN x FLU",
            }
        }
        """
        try:
            partidas_payload = self._get_json_or_none(
                f"{CARTOLA_BASE}/partidas/{rodada}",
                timeout=timeout,
            )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"Não foi possível buscar partidas da rodada {rodada}: {e}")
            )
            return {}

        if not partidas_payload:
            self.stdout.write(
                self.style.WARNING(f"partidas/{rodada} retornou vazio; confronto real não será salvo.")
            )
            return {}

        partidas = partidas_payload.get("partidas") or []
        clubes = partidas_payload.get("clubes") or {}

        if not partidas or not clubes:
            self.stdout.write(
                self.style.WARNING(f"Payload de partidas/{rodada} sem dados suficientes.")
            )
            return {}

        clubes_map = {}
        for clube_id, clube_data in clubes.items():
            try:
                clubes_map[int(clube_id)] = clube_data
            except (TypeError, ValueError):
                continue

        match_map = {}

        for partida in partidas:
            casa_id = partida.get("clube_casa_id")
            fora_id = partida.get("clube_visitante_id")

            if not casa_id or not fora_id:
                continue

            casa = clubes_map.get(casa_id, {})
            fora = clubes_map.get(fora_id, {})

            casa_abv = casa.get("abreviacao") or casa.get("nome_fantasia") or casa.get("nome") or str(casa_id)
            fora_abv = fora.get("abreviacao") or fora.get("nome_fantasia") or fora.get("nome") or str(fora_id)

            match_display = f"{casa_abv} x {fora_abv}"

            match_map[casa_id] = {
                "opponent": fora_abv,
                "is_home": True,
                "match_display": match_display,
            }
            match_map[fora_id] = {
                "opponent": casa_abv,
                "is_home": False,
                "match_display": match_display,
            }

        self.stdout.write(
            self.style.SUCCESS(f"Mapa de confrontos montado para {len(match_map)} clubes na rodada {rodada}.")
        )
        return match_map

    def handle(self, *args, **options):
        draft_id = options["draft_id"]
        week_number = options["week"]
        timeout = options["timeout"]

        draft = (
            Draft.objects.filter(id=draft_id).first()
            if draft_id
            else Draft.objects.order_by("-id").first()
        )

        if not draft:
            self.stderr.write(self.style.ERROR("Nenhum draft encontrado."))
            return

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

        if rodada_atual and week.number != rodada_atual:
            self.stdout.write(
                self.style.WARNING(
                    f"Pulando: Week do app ({week.number}) != rodada_atual do Cartola ({rodada_atual})."
                )
            )
            return

        match_map = self._build_match_map(week.number, timeout=timeout)

        try:
            payload = self._get_json_or_none(
                f"{CARTOLA_BASE}/atletas/pontuados",
                timeout=timeout,
            )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar atletas/pontuados: {e}"))
            return

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

        players = Player.objects.exclude(cartola_id__isnull=True)

        try:
            sample_atleta_id = next(iter(atletas.keys()))
            sample_data = atletas.get(sample_atleta_id) or {}
            sample_points = sample_data.get("pontuacao")
            sample_clube_id = sample_data.get("clube_id")
            self.stdout.write(
                f"Amostra retorno: atleta_id={sample_atleta_id} clube_id={sample_clube_id} pontuacao={sample_points}"
            )
        except StopIteration:
            pass

        upserts = 0
        missing = 0
        club_updates = 0

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

                clube_id = (data or {}).get("clube_id")
                confronto = match_map.get(clube_id, {})

                # 🔥 NOVO: salva o clube do Cartola no Player
                if clube_id and player.cartola_club_id != clube_id:
                    player.cartola_club_id = clube_id
                    player.save(update_fields=["cartola_club_id"])
                    club_updates += 1

                PlayerWeekScore.objects.update_or_create(
                    week=week,
                    player=player,
                    defaults={
                        "points": points,
                        "scouts": scouts,
                        "source": "CARTOLA",
                        "fetched_at": timezone.now(),
                        "opponent": confronto.get("opponent", ""),
                        "is_home": confronto.get("is_home"),
                        "match_display": confronto.get("match_display", ""),
                    },
                )
                upserts += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Sincronização concluída: {upserts} upserts | "
                f"{club_updates} players com cartola_club_id atualizado | "
                f"{missing} atletas no payload sem player correspondente no banco"
            )
        )

        matchups = Matchup.objects.filter(week=week).select_related("home_team", "away_team")

        updated_matchups = 0
        for m in matchups:
            home_pts = compute_team_points(week, m.home_team)
            away_pts = compute_team_points(week, m.away_team)

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