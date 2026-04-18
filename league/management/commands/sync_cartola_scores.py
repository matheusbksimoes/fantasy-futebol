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
        parser.add_argument("--draft-id", type=int, default=None)
        parser.add_argument("--week", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=15)

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

            casa_abv = (
                casa.get("abreviacao")
                or casa.get("nome_fantasia")
                or casa.get("nome")
                or str(casa_id)
            )
            fora_abv = (
                fora.get("abreviacao")
                or fora.get("nome_fantasia")
                or fora.get("nome")
                or str(fora_id)
            )

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

    def _upsert_player_week_score(self, *, week, player, points, scouts, confronto):
        """
        Atualiza/salva PlayerWeekScore e infere live_status com base em mudanças recentes:
        - pending: ainda não pontuou
        - live: pontuação mudou nesta coleta OU já pontuou e ainda não ficou parada 3 coletas
        - finished: pontuação > 0 e ficou 3 coletas seguidas sem mudar
        """
        now = timezone.now()
        new_points = Decimal(str(points or 0))

        score_obj, created = PlayerWeekScore.objects.get_or_create(
            week=week,
            player=player,
            defaults={
                "points": new_points,
                "last_points": Decimal("0"),
                "unchanged_polls_count": 0,
                "live_status": "pending" if new_points == 0 else "live",
                "scouts": scouts or {},
                "source": "CARTOLA",
                "opponent": confronto.get("opponent", ""),
                "is_home": confronto.get("is_home"),
                "match_display": confronto.get("match_display", ""),
            },
        )

        if created:
            return

        old_points = score_obj.points or Decimal("0")

        if new_points != old_points:
            score_obj.unchanged_polls_count = 0
            score_obj.live_status = "live"
        else:
            if new_points > 0:
                score_obj.unchanged_polls_count = (score_obj.unchanged_polls_count or 0) + 1

                if score_obj.unchanged_polls_count >= 3:
                    score_obj.live_status = "finished"
                else:
                    score_obj.live_status = "live"
            else:
                score_obj.unchanged_polls_count = 0
                score_obj.live_status = "pending"

        score_obj.last_points = old_points
        score_obj.points = new_points
        score_obj.scouts = scouts or {}
        score_obj.source = "CARTOLA"
        score_obj.opponent = confronto.get("opponent", "")
        score_obj.is_home = confronto.get("is_home")
        score_obj.match_display = confronto.get("match_display", "")
        score_obj.save(update_fields=[
            "last_points",
            "points",
            "unchanged_polls_count",
            "live_status",
            "scouts",
            "source",
            "fetched_at",
            "opponent",
            "is_home",
            "match_display",
        ])

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
                self.style.ERROR("Nenhuma Week encontrada (crie uma Week e marque is_current=True).")
            )
            return

        try:
            status = self._get_json_or_none(f"{CARTOLA_BASE}/mercado/status", timeout=timeout)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar mercado/status: {e}"))
            return

        if not status:
            self.stderr.write(self.style.ERROR("mercado/status retornou vazio (inesperado)."))
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
            payload = self._get_json_or_none(f"{CARTOLA_BASE}/atletas/pontuados", timeout=timeout)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Erro ao buscar atletas/pontuados: {e}"))
            return

        players = Player.objects.exclude(cartola_id__isnull=True)
        players_by_cartola_id = {
            str(p.cartola_id): p for p in players if p.cartola_id is not None
        }

        club_updates = 0
        upserts = 0
        missing = 0

        # Fallback: se não há pontuados, usa atletas/mercado para preencher cartola_club_id
        # e já salva o confronto da rodada atual para aparecer no roster
        if payload is None:
            self.stdout.write(
                self.style.WARNING(
                    "atletas/pontuados retornou 204. Buscando atletas/mercado para atualizar clubes e salvar confronto da rodada."
                )
            )

            try:
                mercado_payload = self._get_json_or_none(f"{CARTOLA_BASE}/atletas/mercado", timeout=timeout)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Erro ao buscar atletas/mercado: {e}"))
                return

            atletas_mercado = mercado_payload.get("atletas") if mercado_payload else []
            if not atletas_mercado:
                self.stdout.write(self.style.WARNING("atletas/mercado sem atletas; nada a atualizar."))
                return

            with transaction.atomic():
                for atleta in atletas_mercado:
                    atleta_id = atleta.get("atleta_id")
                    clube_id = atleta.get("clube_id")

                    player = players_by_cartola_id.get(str(atleta_id))
                    if not player:
                        continue

                    if clube_id and player.cartola_club_id != clube_id:
                        player.cartola_club_id = clube_id
                        player.save(update_fields=["cartola_club_id"])
                        club_updates += 1

                    confronto = match_map.get(clube_id, {})

                    self._upsert_player_week_score(
                        week=week,
                        player=player,
                        points=0,
                        scouts={},
                        confronto=confronto,
                    )
                    upserts += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"Fallback concluído: {upserts} PlayerWeekScore salvos | "
                    f"{club_updates} players com cartola_club_id atualizado."
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
            return

        atletas = payload.get("atletas") or {}
        if not atletas:
            self.stdout.write(
                self.style.WARNING(
                    "Nenhum atleta pontuado retornado (rodada pode não ter iniciado / sem parcial)."
                )
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Cartola retornou {len(atletas)} atletas pontuados."))

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

                if clube_id and player.cartola_club_id != clube_id:
                    player.cartola_club_id = clube_id
                    player.save(update_fields=["cartola_club_id"])
                    club_updates += 1

                self._upsert_player_week_score(
                    week=week,
                    player=player,
                    points=points,
                    scouts=scouts,
                    confronto=confronto,
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