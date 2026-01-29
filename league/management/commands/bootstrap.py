import os
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings

from league.models import Draft, Team, Player


class Command(BaseCommand):
    help = "Carrega fixtures iniciais só se o banco estiver vazio (idempotente)."

    def handle(self, *args, **options):
        # Se já tem dados essenciais, não faz nada
        if Draft.objects.exists() and Team.objects.exists() and Player.objects.exists():
            self.stdout.write(self.style.SUCCESS("bootstrap_data: dados já existem, pulando."))
            return

        # Prioriza data_utf8.json, senão cai pro data.json
        candidates = ["data_utf8.json", "data.json"]
        fixture_path = None
        for name in candidates:
            p = os.path.join(settings.BASE_DIR, name)
            if os.path.exists(p):
                fixture_path = p
                break

        if not fixture_path:
            self.stdout.write(self.style.WARNING("bootstrap_data: nenhum fixture encontrado (data_utf8.json / data.json)."))
            return

        self.stdout.write(f"bootstrap_data: carregando fixture {fixture_path} ...")
        call_command("loaddata", fixture_path, verbosity=1)
        self.stdout.write(self.style.SUCCESS("bootstrap_data: fixture carregado com sucesso."))
