# league/management/commands/process_waivers.py
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from league.models import WaiverClaim, TeamBudget
from league.services.roster import add_player_to_roster, drop_player_from_roster, is_player_free_agent, team_owns_player

class Command(BaseCommand):
    help = "Processa waiver claims pendentes (FAAB)."

    def handle(self, *args, **options):
        self.stdout.write("Processando waivers...")

        # Pega a lista de jogadores com claims pendentes
        add_player_ids = (
            WaiverClaim.objects.filter(status=WaiverClaim.Status.PENDING)
            .values_list("add_player_id", flat=True)
            .distinct()
        )

        total_processed = 0

        for pid in add_player_ids:
            total_processed += self._process_player_claims(pid)

        self.stdout.write(self.style.SUCCESS(f"OK. Claims processados em {total_processed} linhas."))

    def _process_player_claims(self, add_player_id: int) -> int:
        """
        Processa todos os claims PENDING para um jogador específico.
        Retorna quantos claims foram marcados (WON/LOST/INVALID).
        """
        with transaction.atomic():
            # Lock nos claims desse jogador (evita duas execuções concorrentes)
            claims = list(
                WaiverClaim.objects
                .select_for_update()
                .filter(status=WaiverClaim.Status.PENDING, add_player_id=add_player_id)
                .select_related("team", "drop_player", "add_player")
                .order_by("-bid", "created_at")
            )

            if not claims:
                return 0

            # Se o jogador já não é FA, todos são inválidos
            if not is_player_free_agent(claims[0].add_player):
                now = timezone.now()
                for c in claims:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Player is no longer a free agent"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            winner = None
            winner_reason = ""

            # Encontra o primeiro claim válido
            for c in claims:
                budget = TeamBudget.objects.select_for_update().get(team=c.team)

                if budget.faab_balance < c.bid:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Insufficient FAAB"
                    c.processed_at = timezone.now()
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                if c.drop_player_id and not team_owns_player(c.team, c.drop_player):
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Drop player not on team roster"
                    c.processed_at = timezone.now()
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                # Se você tem regra de limite de roster:
                # if roster_is_full(c.team) and not c.drop_player_id:
                #     c.status = WaiverClaim.Status.INVALID
                #     c.invalid_reason = "Roster full and no drop specified"
                #     c.processed_at = timezone.now()
                #     c.save(...)
                #     continue

                winner = c
                break

            now = timezone.now()

            if winner is None:
                # Se ninguém foi válido, os que restaram PENDING precisam virar INVALID
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "No valid claim for this player"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # Aplica vencedor
            budget = TeamBudget.objects.select_for_update().get(team=winner.team)
            budget.faab_balance -= winner.bid
            budget.save(update_fields=["faab_balance"])

            # Drop (se houver) e Add
            if winner.drop_player_id:
                drop_player_from_roster(winner.team, winner.drop_player)

            add_player_to_roster(winner.team, winner.add_player)

            winner.status = WaiverClaim.Status.WON
            winner.processed_at = now
            winner.invalid_reason = ""
            winner.save(update_fields=["status", "processed_at", "invalid_reason"])

            # Restantes: LOST (exceto os já INVALID)
            for c in claims:
                if c.id == winner.id:
                    continue
                if c.status == WaiverClaim.Status.PENDING:
                    c.status = WaiverClaim.Status.LOST
                    c.processed_at = now
                    c.save(update_fields=["status", "processed_at"])

            return len(claims)
