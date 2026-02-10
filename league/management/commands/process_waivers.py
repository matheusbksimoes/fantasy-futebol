# league/management/commands/process_waivers.py
from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.db.models import Max, Min, F, Window
from django.db.models.functions import RowNumber
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

        # Loop: enquanto existir claim pendente, processa em “rodadas”
        # Em cada rodada:
        # 1) pega 1 claim ATIVO por time (o mais antigo: created_at, id)
        # 2) dentre esses ativos, escolhe qual jogador processar primeiro por MAIOR BID
        while True:
            # ✅ 1 claim ativo por time (o primeiro da “fila” do time)
            active_claims = (
                WaiverClaim.objects
                .filter(status=WaiverClaim.Status.PENDING)
                .annotate(
                    rn=Window(
                        expression=RowNumber(),
                        partition_by=[F("team_id")],
                        order_by=[F("created_at").asc(), F("id").asc()],
                    )
                )
                .filter(rn=1)
            )

            # Se não tem mais ativos, acabou
            if not active_claims.exists():
                break

            # ✅ escolhe qual jogador processar primeiro (entre os ATIVOS)
            next_group = (
                active_claims
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

            add_player_id = next_group["add_player_id"]

            total_processed += self._process_player_claims_active_only(add_player_id)

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

        qs = (
            WaiverClaim.objects
            .select_for_update()
            .filter(
                status=WaiverClaim.Status.PENDING,
                team_id=winner.team_id,
                drop_player_id=winner.drop_player_id,
            )
            .exclude(id=winner.id)
        )

        updated = qs.update(
            status=WaiverClaim.Status.INVALID,
            invalid_reason="Você já usou este jogador como drop em outro claim vencedor",
            processed_at=now,
        )
        return updated or 0

    def _process_player_claims_active_only(self, add_player_id: int) -> int:
        """
        Processa apenas os claims ATIVOS (1 por time) para um jogador específico.
        Retorna quantos claims foram marcados (WON/LOST/INVALID).
        """
        with transaction.atomic():
            now = timezone.now()

            # ✅ recomputa ativos dentro da transaction (evita corrida)
            active_claims = (
                WaiverClaim.objects
                .select_for_update()
                .filter(status=WaiverClaim.Status.PENDING)
                .annotate(
                    rn=Window(
                        expression=RowNumber(),
                        partition_by=[F("team_id")],
                        order_by=[F("created_at").asc(), F("id").asc()],
                    )
                )
                .filter(rn=1, add_player_id=add_player_id)
                .select_related("team", "add_player", "drop_player")
            )

            claims = list(active_claims)
            if not claims:
                return 0

            # Se o jogador já não é FA, esses ativos viram INVALID
            if not is_player_free_agent(claims[0].add_player):
                for c in claims:
                    c.status = WaiverClaim.Status.INVALID
                    c.invalid_reason = "Player is no longer a free agent"
                    c.processed_at = now
                    c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # budgets com lock para os times envolvidos nesses claims ativos
            team_ids = {c.team_id for c in claims}
            budgets = {
                b.team_id: b
                for b in TeamBudget.objects.select_for_update().filter(team_id__in=team_ids)
            }
            for tid in team_ids:
                if tid not in budgets:
                    b, _ = TeamBudget.objects.get_or_create(team_id=tid)
                    budgets[tid] = TeamBudget.objects.select_for_update().get(team_id=tid)

            # valida claims ativos
            valid_claims = []
            for c in claims:
                budget = budgets[c.team_id]

                if budget.faab_balance < c.bid:
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

            if not valid_claims:
                return len(claims)

            # winner: maior bid, empate: menor waiver_priority, depois created_at
            max_bid = max(c.bid for c in valid_claims)
            top = [c for c in valid_claims if c.bid == max_bid]

            used_tiebreak = False
            if len(top) == 1:
                winner = top[0]
            else:
                used_tiebreak = True
                winner = sorted(
                    top,
                    key=lambda c: (
                        budgets[c.team_id].waiver_priority,
                        c.created_at,
                        c.id,
                    ),
                )[0]

            # re-check: ainda é FA
            if not is_player_free_agent(winner.add_player):
                for c in claims:
                    if c.status == WaiverClaim.Status.PENDING:
                        c.status = WaiverClaim.Status.INVALID
                        c.invalid_reason = "Player is no longer a free agent"
                        c.processed_at = now
                        c.save(update_fields=["status", "invalid_reason", "processed_at"])
                return len(claims)

            # aplica vencedor
            winner_budget = budgets[winner.team_id]
            winner_budget.faab_balance -= winner.bid
            winner_budget.save(update_fields=["faab_balance"])

            if winner.drop_player_id:
                drop_player_from_roster(winner.team, winner.drop_player)
            add_player_to_roster(winner.team, winner.add_player)

            winner.status = WaiverClaim.Status.WON
            winner.processed_at = now
            winner.invalid_reason = ""
            winner.save(update_fields=["status", "processed_at", "invalid_reason"])

            invalidated_count = self._invalidate_other_claims_using_same_drop(winner, now)

            if used_tiebreak:
                self._rotate_waiver_priority_after_tiebreak(winner.team_id)

            # restantes ATIVOS desse jogador: LOST (somente os que ainda estão PENDING)
            for c in claims:
                if c.id == winner.id:
                    continue
                if c.status == WaiverClaim.Status.PENDING:
                    c.status = WaiverClaim.Status.LOST
                    c.processed_at = now
                    c.save(update_fields=["status", "processed_at"])

            return len(claims) + invalidated_count
