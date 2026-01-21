import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import Player

# Mapa comum de posições do Cartola
CARTOLA_POS_MAP = {
    1: "GOL",
    2: "LAT",
    3: "ZAG",
    4: "MEI",
    5: "ATA",
    6: "TEC",
}


class Command(BaseCommand):
    help = "Importa/atualiza jogadores do Cartola no banco local."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            type=str,
            default="https://api.cartola.globo.com/atletas/mercado",
            help="Endpoint do Cartola para atletas. Default: atletas/mercado",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        url = options["url"]
        self.stdout.write(f"Buscando atletas em: {url}")

        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()

        atletas = data.get("atletas", [])
        clubes = data.get("clubes", {})

        created = 0
        updated = 0
        skipped = 0

        for a in atletas:
            cartola_id = a.get("atleta_id")
            if cartola_id is None:
                skipped += 1
                continue

            name = (a.get("apelido") or a.get("nome") or "").strip()
            if not name:
                skipped += 1
                continue

            pos_id = a.get("posicao_id")
            position = CARTOLA_POS_MAP.get(pos_id)
            if not position:
                skipped += 1
                continue

            clube_id = a.get("clube_id")
            real_team = ""
            if clube_id is not None:
                club_obj = clubes.get(str(clube_id)) or clubes.get(clube_id)
                if club_obj:
                    real_team = (club_obj.get("nome_fantasia") or club_obj.get("nome") or "").strip()

            obj, was_created = Player.objects.update_or_create(
                cartola_id=cartola_id,
                defaults={
                    "name": name,
                    "position": position,
                    "real_team": real_team,
                },
            )

            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Import finalizado. Criados: {created} | Atualizados: {updated} | Ignorados: {skipped}"
        ))
