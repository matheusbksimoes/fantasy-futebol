import requests

from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import Draft, Week, PlayerWeekScore

CARTOLA_BASE = "https://api.cartola.globo.com"


class Command(BaseCommand):
    help = "Preenche confronto real e mando em PlayerWeekScore já existentes."

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
            help="Se informado, processa só essa week",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=15,
            help="Timeout HTTP em segundos",
        )

    def _get_json(self, url: str, timeout: int):
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _build_match_map(self, rodada: int, timeout: int):
        payload = self._get_json(f"{CARTOLA_BASE}/partidas/{rodada}", timeout=timeout)

        partidas = payload.get("partidas") or []
        clubes = payload.get("clubes") or {}

        clubes_map = {}
        for clube_id, clube_data in clubes.items():
            try:
                clubes_map[int(clube_id)] = clube_data
            except (ValueError, TypeError):
                continue

        match_map = {}

        for partida in partidas:
            casa_id = partida.get("clube_casa_id")
            fora_id = partida.get("clube_visitante_id")

            if not casa_id or not fora_id:
                continue

            casa = clubes_map.get(casa_id, {})
            fora = clubes_map.get(fora_id, {})

            casa_abv = casa.get("abreviacao") or casa.get("nome_fantasia") or casa.get("nome")
            fora_abv = fora.get("abreviacao") or fora.get("nome_fantasia") or fora.get("nome")

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

        weeks = Week.objects.filter(draft=draft).order_by("number")
        if week_number:
            weeks = weeks.filter(number=week_number)

        updated_total = 0

        for week in weeks:
            scores = (
                PlayerWeekScore.objects
                .filter(week=week)
                .select_related("player", "week")
            )

            if not scores.exists():
                continue

            try:
                match_map = self._build_match_map(week.number, timeout=timeout)
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f"Falha ao buscar partidas da rodada {week.number}: {e}")
                )
                continue

            updated_week = 0

            with transaction.atomic():
                for score in scores:
                    # 🔥 AGORA USA ID DO CLUBE (CORRETO)
                    clube_id = score.player.cartola_club_id

                    if not clube_id:
                        continue

                    info = match_map.get(clube_id)

                    if not info:
                        continue

                    changed = False

                    if score.opponent != info["opponent"]:
                        score.opponent = info["opponent"]
                        changed = True

                    if score.is_home != info["is_home"]:
                        score.is_home = info["is_home"]
                        changed = True

                    if score.match_display != info["match_display"]:
                        score.match_display = info["match_display"]
                        changed = True

                    if changed:
                        score.save(update_fields=["opponent", "is_home", "match_display"])
                        updated_week += 1

            updated_total += updated_week

            self.stdout.write(
                self.style.SUCCESS(
                    f"Rodada {week.number}: {updated_week} PlayerWeekScore atualizados."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(f"Backfill concluído. Total atualizado: {updated_total}.")
        )