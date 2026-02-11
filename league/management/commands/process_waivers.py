# league/management/commands/process_waivers.py
from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.db.models import Max, Min
from django.utils import timezone

from league.models import WaiverClaim, TeamBudget
from league.services.roster import (
    add_player_to_roster,
    drop_player_from_roster,
    is_player_free_agent,
    team_owns_player,
)


class Command(BaseCommand):
    help = "Processa waiver claims pendentes (FAAB)."

    def handle(self, *args, **options):
        self.stdout.write("Processando waivers...")

        total_processed = 0

        # Processa por "grupos de jogador":
        # Sempre pega o PRÓXIMO jogador com MAIOR BID entre TODOS os PENDING.
        # Isso garante: maior lance válido vence, independente da ordem.
        while True:
            next_group = (
                WaiverClaim.objects
                .filter(status=WaiverClaim.Status.PENDING)
                .values("add_player_id")
                .annotate(
                    max_bid=Max("bid"),
                    earliest_created_at=Min("created_at"),
                )
                .order_by("-max_bid", "earliest_created_at", "add_player_id")
                .first()
            )

            if not next_group:
                break

            add_player_id = int(next_group["add_player_id"])
            total_processed += self._process_player_claims_all_pending(add_player_id)

        self.stdout.write(self.style.SUCCESS(f"OK. Claims processados em {total_processed} linhas."))

    def _rotate_waiver_priority_after_tiebreak(self, winner_team_id: int):
        """
        Regra:
        - O time que levou jogador NO DESEMPATE vai para último na fila.
        - Todos que estavam atrás dele sobem uma posição (priority - 1).
        """
        winner_budget = TeamBudget.objects.select_for_update().get(team_id=winner_team_id)
        winner_prio = winner_budget.waiver_priority

        max_prio = TeamBudget.objects.aggregate(m=Max("waiver_priority"))["m"] or 0
        if max_prio < 1:
            max_prio = 1

        TeamBudget.objects.filter(waiver_priority__gt=winner_prio).update(
            waiver_priority=models.F("waiver_priority") - 1
        )

        winner_budget.waiver_priority = max_prio
        winner_budget.save(update_fields=["waiver_priority"])

    def _invalidate_other_claims_using_same_drop(self, winner: WaiverClaim, now):
        """
        Se o time ganhou um claim usando drop_player X,
        qualquer OUTRO claim PENDING do mesmo time usando o MESMO drop_player X
        vira INVALID (não dá pra dropar o mesmo jogador duas vezes).
        """
        if not winner.drop_player_id:
            return 0

        updated = (
            WaiverClaim.objects
            .select_for_update()
            .filter(
                status=WaiverClaim.Status.PENDING,
                team_id=winner.team_id,
                drop_player_id=winner.drop_player_id,
            )
            .exclude(id=winner.id)
            .update(
                status=WaiverClaim.Status.INVALID,
                invalid_reason="Você já usou este jogador como drop em outro claim vencedor",
                processed_at=now,
            )
        )
        return updated or 0

    def _process_player_claims_all_pending(self, add_player_id: int) -> int:
        """
        Processa TODOS os claims PENDING para um jogador específico.
        Regra: maior bid válido vence. Empate: waiver_priority (menor) -> created_at -> id.
        Retorna quantos claims foram marcados (WON/LOST/INVALID) + invalidações extras.
        """
        with transaction.atomic():
            now = timezone.now()

            # 1) Trava SOMENTE linhas WaiverClaim (sem JOIN) deste jogador
            locked_rows = list(
                WaiverClaim.objects
                .select_for_update()
                .filter(status=WaiverClaim.Status.PENDING, add_player_id=add_player_id)
                .values("id", "team_id")
                .order_by("-bid", "created_at", "id")
            )

            if not locked_rows:
                return 0

            claim_ids = [r["id"] for r in locked_rows]

            # 2) Agora carrega objetos completos (join ok porque lock já aconteceu)
            claims = list(
                WaiverClaim.objects
                .filter(id__in=claim_ids)
                .select_related("team", "add_player", "drop_player")
                .order_by("-bid", "created_at", "id")
            )
            if not claims:
                return 0

            player_obj = claims[0].add_player

            # Se já não é FA, TODO mundo vira INVALID
            if not is_player_free_agent(player_obj):
                for c in claims:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Player is no longer a free agent"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # 3) Trava budgets dos times envolvidos
            team_ids = {c.team_id for c in claims}
            budgets = {
                b.team_id: b
                for b in TeamBudget.objects.select_for_update().filter(team_id__in=team_ids)
            }
            for tid in team_ids:
                if tid not in budgets:
                    TeamBudget.objects.get_or_create(team_id=tid, defaults={"faab_balance": 100})
                    budgets[tid] = TeamBudget.objects.select_for_update().get(team_id=tid)

            # 4) Valida cada claim (FAAB + drop no roster quando informado)
            valid_claims = []
            for c in claims:
                budget = budgets[c.team_id]

                if int(budget.faab_balance or 0) < int(c.bid or 0):
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Insufficient FAAB"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                if c.drop_player_id and not team_owns_player(c.team, c.drop_player):
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Drop player not on team roster"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                    continue

                valid_claims.append(c)

            # Se ninguém válido, acabou (já marcou INVALID os inválidos)
            if not valid_claims:
                return len(claims)

            # 5) Escolhe vencedor: maior bid, empate por waiver_priority, depois created_at, depois id
            max_bid = max(int(c.bid or 0) for c in valid_claims)
            top = [c for c in valid_claims if int(c.bid or 0) == max_bid]

            used_tiebreak = False
            if len(top) == 1:
                winner = top[0]
            else:
                used_tiebreak = True
                winner = sorted(
                    top,
                    key=lambda c: (
                        int(budgets[c.team_id].waiver_priority or 10**9),
                        c.created_at,
                        c.id,
                    ),
                )[0]

            # Re-check: ainda é FA (segurança extra)
            if not is_player_free_agent(winner.add_player):
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "Player is no longer a free agent"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # 6) Aplica vencedor
            winner_budget = budgets[winner.team_id]
            winner_budget.faab_balance = int(winner_budget.faab_balance or 0) - int(winner.bid or 0)
            winner_budget.save(update_fields=["faab_balance"])

            if winner.drop_player_id:
                drop_player_from_roster(winner.team, winner.drop_player)
            add_player_to_roster(winner.team, winner.add_player)

            winner.status = WaiverClaim.Status.WON
            winner.processed_at = now
            winner.invalid_reason = ""
            winner.save(update_fields=["status", "processed_at", "invalid_reason"])

            # Invalida outros claims do MESMO time usando o MESMO drop
            invalidated_count = self._invalidate_other_claims_using_same_drop(winner, now)

            # Rotação só se houve desempate
            if used_tiebreak:
                self._rotate_waiver_priority_after_tiebreak(winner.team_id)

            # 7) Restantes: se ainda PENDING aqui, vira LOST (disputaram e perderam)
            for c in claims:
                if c.id == winner.id:
                    continue
                if c.status == WaiverClaim.Status.PENDING:
                    c.status = WaiverClaim.Status.LOST
                    c.processed_at = now
                    c.save(update_fields=["status", "processed_at"])

            return len(claims) + invalidated_count
